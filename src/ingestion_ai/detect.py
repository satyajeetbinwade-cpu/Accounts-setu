"""Deterministic, zero-model **upload-slot detection**.

Answers "which slot does this file belong to?" from the file's NAME and its
detected COLUMN HEADERS, so an upload screen can pre-select the right slot and
the user only has to override it when the guess is wrong.

WHY THIS IS NOT THE MODEL CLASSIFIER
------------------------------------
``normalizer.classify_document`` is the authoritative classifier, but it is a
live model call (~15-25s) and it runs *inside* ``normalize_source_file`` at
ingestion time. A drop-zone cannot wait on that: the slot decides which
directory the bytes are written to, so the guess must be instant and free.
This module is therefore a **hint**, never a gate:

* the user can always change the slot in the dropdown (the auto-pick just
  fills it in), and
* the engine still classifies the file at ingestion time, so a wrong hint is
  caught by the same wrong-slot check that has always existed.

It is deliberately confined to two cheap signals — the filename and the header
row — and returns ``None`` (rather than guessing) when neither is decisive.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# A source type's signals. ``filename`` tokens are weighted heavily because a
# GSTN/bank/ERP export almost always names its own document; ``headers`` are
# the corroborating structural evidence used when the name is uninformative
# (e.g. a client exports "Sheet1" from Tally).
#
# Every token is written ALREADY NORMALIZED (lowercase, non-alphanumerics
# collapsed to a single space) and matched as a substring of the normalized
# filename / header label.
_DETECT_RULES: dict[str, dict[str, list[str]]] = {
    # --- portal (GST) -------------------------------------------------
    "gstr2b": {
        "filename": ["gstr2b", "gstr 2b"],
        "headers": [
            "invoice details invoice number",
            "invoice details invoice value",
            "credit note debit note details note number",
        ],
        "strong_headers": [
            "itc availability",
            "supply attract reverse charge",
            "gstr 1 iff gstr 5 period",
        ],
    },
    "ims": {
        "filename": ["ims"],
        "headers": ["source return period"],
        "strong_headers": [
            "ims action",
            "amount declared by taxpayer for itc reduction",
        ],
    },
    # --- books side ---------------------------------------------------
    # The note register is checked before the Purchase Register: both are
    # books-side, and a "Credit Note Register" filename also contains the
    # generic word "register", so the more specific signals must win.
    "credit_notes": {
        "filename": ["credit note", "debit note", "note register"],
        "headers": ["voucher type", "voucher ref"],
        "strong_headers": [
            "unclaimed cgst",
            "unclaimed sgst",
            "debit note register",
            "credit note register",
        ],
    },
    "tally": {
        "filename": [
            "purchase register", "sales register", "bill wise", "tally", "purchase",
        ],
        "headers": ["bill no", "bill amount"],
        "strong_headers": ["name address of dealer", "igst tax amt", "cgst tax amt", "sgst tax amt"],
    },
    # --- 2C / other sources -------------------------------------------
    "form26as": {
        "filename": ["26as", "form26as", "form 26as"],
        "headers": ["certificate number"],
        "strong_headers": ["deductor", "tax deposited"],
    },
    "tds": {
        "filename": ["tds return", "tds challan", "challan"],
        "headers": ["section"],
        "strong_headers": ["deductee", "tds section"],
    },
    "bank": {
        "filename": ["bank", "statement"],
        "headers": ["deposit", "cheque", "transaction date"],
        "strong_headers": ["withdrawal", "closing balance"],
    },
    "vendor_ledger": {
        "filename": ["vendor ledger", "supplier ledger"],
        "headers": ["vendor", "supplier"],
        "strong_headers": ["closing balance"],
    },
    "opening_balances": {
        "filename": ["opening balance", "opening balances"],
        "headers": [],
        "strong_headers": ["opening balance", "opening debit", "opening credit"],
    },
    "loan_sheet": {
        "filename": ["loan sheet", "loan statement", "loan"],
        "headers": ["emi", "principal"],
        "strong_headers": ["loan account", "interest amount"],
    },
    "salary": {
        "filename": ["salary register", "salary"],
        "headers": ["basic", "hra"],
        "strong_headers": ["net pay", "gross salary", "employee code"],
    },
}

# Filename evidence is a much stronger signal than a single header token.
_FILENAME_WEIGHT = 3
# A single generic header hit is too weak on its own (many ledgers share
# "date" / "deposit"), but ONE distinctive header (e.g. IMS's ITC-reduction
# block) is decisive — so a strong header counts as two.
_STRONG_HEADER_WEIGHT = 2


def _norm(text: str) -> str:
    """Lowercase, collapse every non-alphanumeric run to a single space."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _score(tokens: list[str], haystack: str) -> int:
    return sum(1 for tok in tokens if tok and tok in haystack)


def detect_source_type(
    filename: str, headers: list[str], *, allowed: Optional[set[str]] = None,
) -> tuple[Optional[str], str]:
    """Best-guess source-type key for a file, with a one-line reason.

    ``allowed`` restricts the candidates to the slots actually offered in the
    current context (e.g. only ``tally`` + ``gstr2b`` + ``ims`` for a GST run).
    Returns ``(None, "")`` when the evidence is not decisive, so the caller
    leaves the user's own choice untouched.
    """
    name = _norm(filename)
    header_blob = " ".join(_norm(h) for h in (headers or []))

    scored: list[tuple[str, int, int, int]] = []
    for key, rules in _DETECT_RULES.items():
        if allowed is not None and key not in allowed:
            continue
        fn = _score(rules.get("filename", []), name)
        hd = _score(rules.get("headers", []), header_blob)
        sh = _score(rules.get("strong_headers", []), header_blob)
        header_score = hd + sh * _STRONG_HEADER_WEIGHT
        total = fn * _FILENAME_WEIGHT + header_score
        scored.append((key, total, fn, header_score))

    if not scored:
        return None, ""

    scored.sort(key=lambda t: (-t[1], -t[2], t[0]))
    best_key, best_total, best_fn, best_header = scored[0]

    # Decisive only when the name names the document, or the headers carry a
    # distinctive token (strong header = 2, so a lone strong hit qualifies).
    if best_total == 0 or (best_fn == 0 and best_header < _STRONG_HEADER_WEIGHT):
        return None, ""

    # An exact tie between two candidates means the evidence does not
    # distinguish them — refuse to guess.
    if len(scored) > 1 and scored[1][1] == best_total and scored[1][2] == best_fn:
        return None, ""

    if best_fn:
        reason = f"the filename says “{filename}”"
    else:
        reason = "the column headers match this layout"
    return best_key, reason


def detect_source_type_from_path(
    filename: str, path: str, *, allowed: Optional[set[str]] = None,
) -> tuple[Optional[str], str]:
    """Read the header row from a file on disk, then detect the source type.

    Never raises: an unreadable/unparseable file simply yields ``(None, "")``
    so a detection failure can never block an upload.
    """
    if not path or not Path(path).exists():
        return None, ""
    try:
        from src.ingestion_ai import normalizer

        raw = normalizer.read_raw_with_header_detection(Path(path))
        headers = [str(c) for c in raw.df.columns]
    except Exception:  # noqa: BLE001
        return None, ""
    return detect_source_type(filename, headers, allowed=allowed)
