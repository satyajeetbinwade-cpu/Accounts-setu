"""Note identity — reference extraction, normalisation and kind classification.

WHY THIS EXISTS
---------------
A books-side Credit/Debit Note Register does not carry the supplier's note
number where you would expect it. In the reference file (sheet "Debit Note
Register") the dedicated ``Voucher Ref. No.`` column is BLANK on every row —
its only content is the client's own booking reference — so the only usable
signal is free text inside ``Narration``:

    "CREDIT NOTE NO.  CD-12/04-2025"     double space, "NO."
    "CREDIT NOTE NO. SRI-312/25-26"      FY-style suffix on the reference
    "CREDIT NOTE NO. 50"                 bare number, no suffix at all
    "Credit Note No CD-197/04-2025"      different case, no period
    "CREDIT NOTE NO. CN/193/25-26"       the reference itself contains "CN/"

A SINGLE client's own file mixes all of those, so extraction has to survive
spacing, case and punctuation variation without being tuned per row.

TWO RULES THIS MODULE ENCODES — both driven by config
(``config/matching_rules.yaml`` -> ``gst.notes``), never hardcoded:

1. THE VOUCHER TYPE IS NOT AUTHORITATIVE. The reference file's ``Voucher Type``
   reads "Debit Note" on EVERY row, yet every row cites a supplier CREDIT note.
   A "Debit Note" voucher is how Tally records a credit note the client
   RECEIVED, so classifying by the label would get 100% of the rows wrong. A
   row is a credit note when it CITES one; the label is kept only as audit
   evidence (``voucher_type_raw``). A row that cites nothing falls back to
   today's genuine-debit-note treatment.

2. NORMALISATION IS SHARED BY BOTH SIDES. The books register and the portal's
   ``B2B-CDNR`` note must reduce to the SAME join key, so one function is used
   for both, and the raw string is kept alongside it as evidence.

   The period patterns are deliberately NOT the invoice ones.
   ``invoice_number_fy_patterns`` is UNANCHORED, so
   ``(?<![0-9])[0-9]{2}/[0-9]{2}(?![0-9])`` matches the INTERIOR "12/04" of
   "CD-12/04-2025" and would collapse "CD-12/04-2025" and "CD-12/05-2025" onto
   the SAME key — silently merging two different supplier notes. Note
   references therefore strip period tokens only when they sit at the END.

Pure functions only: no I/O, no pandas, no DB. The caller supplies the row's
values and the ``gst.notes`` config block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

# --- Document kinds --------------------------------------------------------

CREDIT_NOTE = "Credit Note"
DEBIT_NOTE = "Debit Note"

# --- Why a row was classified as it was ------------------------------------
# Retained on the result so the report (and the tests) can state the reason
# rather than just the verdict.
SIGNAL_CITATION = "citation"                        # narration cites a credit note
SIGNAL_DEDICATED_REFERENCE = "dedicated_reference"   # a populated ref column
SIGNAL_CN_MARKER = "cn_marker"                       # CN- / CN/ prefix
SIGNAL_NEGATIVE_VALUE = "negative_value"             # sign convention
SIGNAL_NONE = "none"                                 # genuine debit note

# A reference always carries at least one digit. Requiring it stops a stray
# word right after the citation ("CREDIT NOTE DATED ...") from being harvested
# as a reference. A genuinely letters-only reference is reported as
# unextractable instead — never silently invented.
_REFERENCE_TOKEN = r"((?=[\w./\-]*\d)[A-Za-z0-9][\w./\-]*)"

# Looks like a bare date rather than a note number — warned about, not blocked.
_BARE_DATE_RE = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")

# --- Defaults --------------------------------------------------------------
# Kept here so the module is usable (and testable) without a config block.
# `gst.notes` in matching_rules.yaml carries the live values and MUST stay
# identical to these so behaviour does not depend on whether config was passed.
DEFAULT_CITATION_PATTERNS: tuple[str, ...] = (
    r"(?i)\bcredit\s*note\b",
    r"(?i)\bc\.?\s?n\.?\s*(?:no|number|#|:)",
)
DEFAULT_EXTRACT_PATTERN = (
    r"(?i)(?:no\.?|number|#|:)?\s*[.:\-]?\s*" + _REFERENCE_TOKEN
)
DEFAULT_CN_MARKER_PATTERNS: tuple[str, ...] = (r"(?i)^CN\s*[-/]",)

DEFAULT_NORMALISATION: dict[str, Any] = {
    "uppercase": True,
    "strip_trailing": r"[-./\s]+$",
    "trailing_period_patterns": [
        r"(?<=\w)[-/\s]?[0-9]{2}-[0-9]{2}$",
        r"(?<=\w)[-/\s]?[0-9]{4}-[0-9]{2}$",
    ],
    "strip_separators": r"[-/.\s]",
    "strip_leading_zeros_in_numeric_runs": True,
}


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoteIdentity:
    """What one books register row's note identity is, and why.

    ``reference`` is the JOIN KEY (normalised, shared with the portal side);
    ``reference_raw`` is the verbatim cited string, kept as evidence.
    """

    kind: str                             # CREDIT_NOTE | DEBIT_NOTE
    signal: str                           # one of the SIGNAL_* constants
    reference_raw: Optional[str]          # verbatim token, for evidence
    reference: Optional[str]              # normalised join key
    voucher_type_raw: str                 # the register's own label (audit only)
    citation_count: int                   # >1 => the row cites several notes (Q10)
    source: str                           # "dedicated_column" | "narration" | ""
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------


def _patterns(value: Any) -> list[str]:
    """Coerce a config value into a list of regex strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v or "").strip()]
    return []


def _sub(pattern: Optional[str], text: str, repl: str) -> str:
    """``re.sub`` that never raises on a malformed configured pattern.

    A bad regex in config must degrade to "this one step did nothing", not
    take the whole run down — the run's own key-collision report is what
    makes a bad pattern visible.
    """
    if not pattern:
        return text
    try:
        return re.sub(pattern, repl, text)
    except re.error:
        return text


def _search(pattern: Optional[str], text: str) -> bool:
    if not pattern or not text:
        return False
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return False


def _first_group(match: re.Match) -> str:
    """First NON-EMPTY capture group, else the whole match.

    Mirrors `src/invoice_extract/extractor.py::_extract_from_text` so a
    configured pattern may carry alternation groups without the caller
    having to guarantee group numbering.
    """
    if match.lastindex:
        for i in range(1, match.lastindex + 1):
            group = match.group(i)
            if group:
                return group
    return match.group(0)


def _citation_spans(text: str, patterns: list[str]) -> list[tuple[int, int]]:
    """Where, and how many times, the text cites a note — overlap-merged.

    Several configured patterns can hit the SAME phrase, so raw match counts
    would over-report ("N credit notes cited" when there is one). Overlapping
    spans are merged before counting.
    """
    spans: list[tuple[int, int]] = []
    for pattern in patterns:
        try:
            spans.extend((m.start(), m.end()) for m in re.finditer(pattern, text))
        except re.error:
            continue
    if not spans:
        return []

    spans.sort()
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:                 # overlapping or touching => one citation
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def citation_count(text: Any, patterns: Any = None) -> int:
    """How many distinct credit-note citations the text makes (0 when none)."""
    body = str(text or "")
    if not body:
        return 0
    pats = _patterns(patterns) or list(DEFAULT_CITATION_PATTERNS)
    return len(_citation_spans(body, pats))


def extract_note_reference(
    text: Any,
    patterns: Any = None,
    extract_pattern: Optional[str] = None,
) -> Optional[str]:
    """The reference token cited by the FIRST citation in ``text``, or None.

    Extraction runs on the text AFTER the first citation — never on the whole
    string — because an unanchored pattern would otherwise be free to match a
    bare word ("CREDIT") when no reference follows. When several notes are
    cited, the first wins (Q10) and the caller records the raw narration.
    """
    body = str(text or "")
    if not body:
        return None
    pats = _patterns(patterns) or list(DEFAULT_CITATION_PATTERNS)
    spans = _citation_spans(body, pats)
    if not spans:
        return None

    tail = body[spans[0][1]:]
    pattern = extract_pattern or DEFAULT_EXTRACT_PATTERN
    try:
        match = re.search(pattern, tail)
    except re.error:
        return None
    if not match:
        return None
    token = _first_group(match).strip()
    return token or None


# ---------------------------------------------------------------------------
# Normalisation (shared by BOTH sides)
# ---------------------------------------------------------------------------


def normalise_note_reference(raw: Any, spec: Optional[dict[str, Any]] = None) -> str:
    """The join key for a note reference — ONE function for books AND portal.

    Order matters: period tokens are matched while the separators are still
    present (`25-26`, `/25-26`), then all separators are removed, then leading
    zeros are dropped from numeric runs. Stripping periods LAST would leave
    nothing to anchor the end patterns on.
    """
    cfg = {**DEFAULT_NORMALISATION, **(spec or {})}
    text = str(raw or "").strip()
    if not text:
        return ""

    if cfg.get("uppercase", True):
        text = text.upper()

    text = _sub(cfg.get("strip_trailing"), text, "")
    for pattern in _patterns(cfg.get("trailing_period_patterns")):
        text = _sub(pattern, text, "")
    # Stripping a period token can expose a fresh trailing separator.
    text = _sub(cfg.get("strip_trailing"), text, "")

    separators = cfg.get("strip_separators")
    if separators:
        text = _sub(separators, text, "")

    if cfg.get("strip_leading_zeros_in_numeric_runs", True):
        text = _sub(r"(?<!\d)0+(\d)", text, r"\1")

    return text


def _is_negative(value: Any) -> bool:
    if value is None or value == "":
        return False
    try:
        return float(str(value).replace(",", "").strip()) < 0
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Classification (the precedence rule)
# ---------------------------------------------------------------------------


def classify_note(
    *,
    reference_column: Any = None,
    narration: Any = None,
    voucher_type: Any = None,
    invoice_value: Any = None,
    taxable_value: Any = None,
    config: Optional[dict[str, Any]] = None,
) -> NoteIdentity:
    """Classify one BOOKS register row as a credit note or a genuine debit note.

    Precedence, in order (see the module docstring for why):

      1. narration/reference CITES a credit note   -> Credit Note  (signal=citation)
      2. a populated dedicated reference column    -> Credit Note  (signal=dedicated_reference)
      3. CN- / CN/ marker                          -> Credit Note  (signal=cn_marker)
      4. negative value                            -> Credit Note  (signal=negative_value)
      5. otherwise                                 -> Debit Note   (signal=none)

    ``reference_column`` must be the SUPPLIER NOTE REFERENCE column only. The
    caller must never pass the client's own ``Voucher No.`` here — that would
    turn a booking reference into a join key. ``voucher_type`` is recorded but
    never consulted for the verdict.
    """
    cfg = config or {}
    citation_patterns = _patterns(cfg.get("citation_patterns")) or list(DEFAULT_CITATION_PATTERNS)
    extract_pattern = cfg.get("reference_extract_pattern") or DEFAULT_EXTRACT_PATTERN
    cn_patterns = _patterns(cfg.get("cn_marker_patterns")) or list(DEFAULT_CN_MARKER_PATTERNS)
    norm_spec = cfg.get("normalise") or {}

    reference_text = str(reference_column or "").strip()
    narration_text = str(narration or "").strip()
    combined = " ".join(part for part in (reference_text, narration_text) if part)

    warnings: list[str] = []

    spans = _citation_spans(combined, citation_patterns) if combined else []
    n_citations = len(spans)

    token: Optional[str] = None
    if n_citations:
        token = extract_note_reference(combined, citation_patterns, extract_pattern)
        if not token:
            warnings.append(
                "A credit note is cited but no reference could be extracted, so the "
                "row cannot be matched on reference."
            )
    if n_citations > 1:
        warnings.append(
            f"{n_citations} credit notes are cited in one row; the first was used. "
            "The raw narration is retained for review."
        )

    # The reference VALUE follows the configured source priority.
    if reference_text:
        raw, source = reference_text, "dedicated_column"
    elif token:
        raw, source = token, "narration"
    else:
        raw, source = None, ""

    if raw and _BARE_DATE_RE.match(raw.strip()):
        warnings.append(
            f"Extracted reference {raw!r} looks like a date rather than a note "
            "number; confirm the narration convention for this client."
        )

    reference = normalise_note_reference(raw, norm_spec) if raw else None
    if raw and not reference:
        warnings.append(f"Reference {raw!r} normalised to an empty key.")

    # --- The verdict ---------------------------------------------------
    if n_citations:
        kind, signal = CREDIT_NOTE, SIGNAL_CITATION
    elif raw:
        kind, signal = CREDIT_NOTE, SIGNAL_DEDICATED_REFERENCE
    elif any(_search(p, s) for p in cn_patterns for s in (reference_text, narration_text) if s):
        kind, signal = CREDIT_NOTE, SIGNAL_CN_MARKER
    elif _is_negative(invoice_value) or _is_negative(taxable_value):
        kind, signal = CREDIT_NOTE, SIGNAL_NEGATIVE_VALUE
    else:
        kind, signal = DEBIT_NOTE, SIGNAL_NONE

    return NoteIdentity(
        kind=kind,
        signal=signal,
        reference_raw=raw,
        reference=reference,
        voucher_type_raw=str(voucher_type or "").strip(),
        citation_count=n_citations,
        source=source,
        warnings=tuple(warnings),
    )
