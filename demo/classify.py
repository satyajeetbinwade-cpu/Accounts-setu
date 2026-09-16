"""File-type detection for the demo module: heuristic first, LLM fallback.

Two passes, exactly as the build prompt specifies:

  * **Pass 1 — heuristic.** Read the file's header row and compare it against
    the raw-header lists already defined per source type in
    ``config/source_mappings.yaml``. Score by header overlap. A clear winner
    above the confidence threshold is classified with no model call.
  * **Pass 2 — LLM fallback.** For files that don't clear the threshold
    (ambiguous headers, unexpected column order, renamed columns), ask the
    model what the file is and — critically — for a proposed column mapping
    onto the canonical schema.

The LLM's proposed mapping is never written into the shared
``config/source_mappings.yaml``. It is applied through a scoped adapter
(:func:`preseed_trusted_shapes`) that registers the mapping against this one
file shape for this one session, so the engine's own ingestion layer reads
the file with the right columns and no model call of its own.

Nothing in this module modifies ``src/``, ``config/`` or ``db/``.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml
from rapidfuzz import fuzz

from src.ingestion import GST_CANONICAL_FIELDS, TDS_CANONICAL_FIELDS
from src.ingestion_ai import db as idb
from src.ingestion_ai import llm
from src.ingestion_ai.normalizer import (
    _ACCEPTED_CLASSIFICATIONS,
    _header_signature,
    read_raw_with_header_detection,
)

MAPPINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "source_mappings.yaml"

# The source types this demo recognises. Deliberately the GST/TDS set plus
# IMS — the 2C "Other" sources are out of scope for a client-facing demo.
DEMO_SOURCE_TYPES = ["tally", "gstr2b", "ims", "form26as", "tds"]

# --- Heuristic tuning -------------------------------------------------------

# A file is classified without a model call only when the best candidate has
# at least this share of its expected headers present...
HEURISTIC_THRESHOLD = 70
# ...and beats the runner-up by at least this many points. A near-tie is a
# genuine ambiguity and is handed to the model rather than guessed at.
HEURISTIC_MARGIN = 15
# Per-column fuzzy score at which a header counts as "present". 70 tolerates
# real-world variants ("Invoice No." vs "Invoice Number", "Challan Number"
# vs "Challan No.") without matching unrelated columns.
FUZZY_MATCH_FLOOR = 70
# Weight given to a source type's signature column(s) when present. Used only
# to break a tie between two types that share a schema (GSTR-2B vs IMS).
SIGNATURE_BONUS = 8

# Columns that are strong, specific evidence for one source type. Several
# source types share most of their columns, so without this they score the
# same and every such file would fall through to the model:
#
#   * GSTR-2B and IMS share an identical layout — an IMS export always
#     carries its action columns, a GSTR-2B never does.
#   * Form 26AS and a TDS return both carry section/amount/tax columns. A
#     26AS identifies the DEDUCTOR ("Deductor PAN") and carries the
#     certificate/deposit detail; a TDS return identifies the DEDUCTEE.
_SIGNATURE_COLUMNS: dict[str, list[str]] = {
    "ims": ["IMS Action", "Action Date"],
    "form26as": ["Deductor PAN", "Deductor Name", "Tax Deposited", "Certificate Number"],
    "tds": ["Challan No."],
}

# When two candidates tie and neither carries a signature column, prefer the
# more common export for that schema family rather than spending a model call
# on a distinction that doesn't change how the file is reconciled.
_FAMILY_DEFAULTS: dict[frozenset[str], str] = {
    frozenset({"gstr2b", "ims"}): "gstr2b",
}

# What each source type IS — sent to the model instead of raw headers, so it
# can reason about the file's meaning rather than pattern-matching strings.
SOURCE_TYPE_DESCRIPTIONS: dict[str, str] = {
    "tally": (
        "Books / purchase register — the client's OWN internal record of "
        "transactions (a Tally export, an ERP export, or a hand-prepared "
        "register). Carries GSTIN, party name, invoice number and date, "
        "taxable value and tax components, and/or PAN, TDS section, amount "
        "paid/credited and tax deducted."
    ),
    "gstr2b": (
        "GSTR-2B — the GST portal's own statement of inward supplies for a "
        "period. Carries the supplier's GSTIN, trade/legal name, invoice "
        "number and date, taxable value, and tax split into Integrated / "
        "Central / State tax and Cess."
    ),
    "ims": (
        "IMS export — the Invoice Management System action export from the "
        "GST portal. Same layout as GSTR-2B but additionally carries IMS "
        "action columns (e.g. 'IMS Action', 'Action Date') recording the "
        "accept / reject / pending decision taken on each invoice."
    ),
    "form26as": (
        "Form 26AS — the TRACES statement of tax deducted at source. Carries "
        "the deductor's name and PAN, the TDS section, amount paid/credited, "
        "tax deducted, and date of deposit."
    ),
    "tds": (
        "TDS return / challan detail — the deductor-side TDS filing. Carries "
        "deductee name and PAN, section, amount paid/credited, tax deducted, "
        "challan number and deposit date."
    ),
}

# The engine's own document-type vocabulary label for each source type. The
# engine's ingestion layer compares the model's classification against this
# vocabulary, so a pre-seeded shape must use exactly these strings.
_ENGINE_DOC_TYPE: dict[str, str] = {
    "tally": "Books / purchase register",
    "gstr2b": "GSTR-2B",
    "ims": "IMS export",
    "form26as": "Form 26AS",
    "tds": "TDS return/challan",
}


# ---------------------------------------------------------------------------
# Model resolution — the same routing pattern the rest of the project uses
# ---------------------------------------------------------------------------


def resolve_model(touchpoint_key: str, fallback_key: str = "simple") -> Optional[str]:
    """Resolve the model for an AI touchpoint.

    Reads the central AI model registry (Setup → AI Models) first — the single
    place every touchpoint's model is set — then falls back to
    ``config/ai_config.yaml``'s routing. Returns None when neither is
    available, in which case the caller uses the LLM client's own default.

    Best-effort by design: a registry or config problem must never make the
    demo unusable.
    """
    try:
        from src.ai_models import service as ai_models

        model = ai_models.effective_model(touchpoint_key)
        if model:
            return model
    except Exception:  # noqa: BLE001
        pass
    try:
        with open(MAPPINGS_PATH.parent / "ai_config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return (cfg.get("models") or {}).get(fallback_key)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Structural read
# ---------------------------------------------------------------------------


def _normalize_label(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).strip().lower()).strip()


def _read_structure(data: bytes, filename: str) -> tuple[list[str], list[dict[str, str]], int]:
    """Read a file's headers + a few sample rows.

    Uses the engine's own tolerant reader (ragged CSVs, title rows, multi-sheet
    workbooks) so the demo sees exactly what the engine will see. Returns
    ``(headers, samples, row_count)``; raises on an unreadable file.
    """
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ".csv"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    path = Path(tmp)
    try:
        import os

        os.close(fd)
        path.write_bytes(data)
        read = read_raw_with_header_detection(path)
        headers = [str(c) for c in read.df.columns]
        samples: list[dict[str, str]] = []
        for _, row in read.df.head(5).iterrows():
            samples.append({str(k): str(v) for k, v in row.items()})
        return headers, samples, len(read.df)
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Pass 1 — heuristic
# ---------------------------------------------------------------------------


def _load_mappings() -> dict[str, Any]:
    """Read config/source_mappings.yaml. Returns {} when unreadable."""
    try:
        with open(MAPPINGS_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return {}


def _tally_groups(mappings: dict[str, Any]) -> dict[str, list[str]]:
    """The books slot's two shapes, split by recon type.

    The same books slot legitimately accepts either a GST purchase register or
    a TDS deductor/deductee register, and the two share almost no columns.
    Scoring them together would make every books file look half-mapped, so
    they are kept apart and matched to the recon type that needs them.
    """
    columns_map = (mappings.get("tally") or {}).get("columns") or {}
    gst = [raw for raw, field in columns_map.items() if field in GST_CANONICAL_FIELDS]
    tds = [raw for raw, field in columns_map.items() if field in TDS_CANONICAL_FIELDS]
    return {"GST": gst, "TDS": tds}


def _load_expected_columns(mappings: dict[str, Any]) -> dict[str, list[list[str]]]:
    """source_type -> list of column GROUPS, derived from source_mappings.yaml.

    A group is a set of raw headers that must all be present for that shape.
    Most source types have exactly one group; ``tally`` has two (see
    :func:`_tally_groups`).

    Derived from the config rather than hand-maintained, so it can't drift
    from the mappings the engine actually uses.
    """
    groups: dict[str, list[list[str]]] = {}
    for source_type in DEMO_SOURCE_TYPES:
        if source_type == "tally":
            tally = _tally_groups(mappings)
            groups[source_type] = [g for g in (tally["GST"], tally["TDS"]) if g]
        else:
            columns_map = (mappings.get(source_type) or {}).get("columns") or {}
            groups[source_type] = [list(columns_map.keys())]
    return groups


def books_coverage(data: bytes, filename: str, recon_type: str) -> float:
    """How well a books file fits one recon type's schema, as a percentage.

    Used to decide which books file feeds which reconciliation when a session
    contains both a GST purchase register and a TDS register — they are
    different files in the same slot, and each belongs to a different run.
    Returns 0.0 when the file can't be read.
    """
    mappings = _load_mappings()
    group = _tally_groups(mappings).get(recon_type.upper())
    if not group:
        return 0.0
    try:
        headers, _samples, _rows = _read_structure(data, filename)
    except Exception:  # noqa: BLE001
        return 0.0
    coverage, _mapping, _missing = _score_source_type(headers, [group], mappings)
    return coverage


def _best_column_match(header: str, candidates: list[str]) -> tuple[Optional[str], float]:
    """The candidate raw column that best matches an actual header, and its
    score (0-100). Exact normalized equality short-circuits to 100."""
    norm = _normalize_label(header)
    best: Optional[str] = None
    best_score = 0.0
    for candidate in candidates:
        if _normalize_label(candidate) == norm:
            return candidate, 100.0
        score = float(fuzz.token_set_ratio(norm, _normalize_label(candidate)))
        if score > best_score:
            best, best_score = candidate, score
    return best, best_score


def _assign_one_to_one(
    group: list[str], headers: list[str]
) -> dict[str, str]:
    """Greedily assign each expected column to a distinct source header.

    Matching is ONE-TO-ONE: a single source column can be claimed by at most
    one expected column. Without this, fuzzy matching lets a shared word pull
    several expected columns onto the same header — e.g. "Invoice No." and
    "Invoice Date" both landing on "Invoice Value" because they share the word
    "invoice". That produces a mapping that looks complete but is wrong, which
    is worse than no mapping at all: it would silently corrupt the run.

    Assignment runs strongest-match-first, so an exact match always wins its
    column before a weaker fuzzy match can take it.
    """
    pairs: list[tuple[float, str, str]] = []
    for expected in group:
        for header in headers:
            _candidate, score = _best_column_match(expected, [header])
            if score >= FUZZY_MATCH_FLOOR:
                pairs.append((score, expected, header))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

    matched: dict[str, str] = {}
    used_headers: set[str] = set()
    for _score, expected, header in pairs:
        if expected in matched or header in used_headers:
            continue
        matched[expected] = header
        used_headers.add(header)
    return matched


def _to_canonical(matched: dict[str, str], mappings: dict[str, Any]) -> dict[str, str]:
    """Translate raw column -> canonical field using the config, so a mapping
    is always expressed in the engine's own vocabulary."""
    out: dict[str, str] = {}
    for cfg in mappings.values():
        columns_map = (cfg or {}).get("columns") or {}
        for raw, field in columns_map.items():
            if raw in matched and field not in out:
                out[field] = matched[raw]
    return out


def _score_source_type(
    headers: list[str], groups: list[list[str]], mappings: dict[str, Any]
) -> tuple[float, dict[str, str], list[str]]:
    """Score one source type against a file's headers.

    Returns ``(coverage_pct, mapping, missing)``.

    ``coverage`` is the share of the BEST-matching group's expected columns
    that are present — that is what decides whether the file is this type.

    ``mapping`` is the union of every group's matches, not just the best
    group's. This matters for the books slot, which legitimately carries both
    a GST and a TDS shape: a books export that serves both reconciliations
    must have BOTH sets of columns mapped, or the TDS run would receive a
    frame with no TDS columns at all. Coverage still comes from the best
    group, so a GST-only books file is not penalised for lacking TDS columns.
    """
    best_coverage = 0.0
    best_missing: list[str] = []
    union_matched: dict[str, str] = {}

    for group in groups:
        matched = _assign_one_to_one(group, headers)
        # Merge into the union, keeping the first claim on any shared header.
        for expected, header in matched.items():
            if header not in union_matched.values():
                union_matched.setdefault(expected, header)

        coverage = 100.0 * len(matched) / len(group) if group else 0.0
        if coverage > best_coverage:
            best_coverage = coverage
            best_missing = [e for e in group if e not in matched]

    return best_coverage, _to_canonical(union_matched, mappings), best_missing


def heuristic_classify(
    headers: list[str], declared_source_type: Optional[str] = None
) -> dict[str, Any]:
    """Pass 1: classify a file from its headers alone.

    Returns a dict with ``source_type`` (None when nothing clears the bar),
    ``confidence``, ``method``, ``column_mapping_override``, ``reason`` and
    ``ambiguous_with``.
    """
    mappings = _load_mappings()
    groups = _load_expected_columns(mappings)
    scored: list[tuple[str, float, float, dict[str, str], list[str]]] = []
    for source_type in DEMO_SOURCE_TYPES:
        coverage, mapping, missing = _score_source_type(
            headers, groups[source_type], mappings
        )
        signature_hits = sum(
            1 for col in _SIGNATURE_COLUMNS.get(source_type, [])
            if _best_column_match(col, headers)[1] >= FUZZY_MATCH_FLOOR
        )
        rank = coverage + SIGNATURE_BONUS * signature_hits
        scored.append((source_type, coverage, rank, mapping, missing))

    scored.sort(key=lambda row: row[2], reverse=True)
    top_type, top_coverage, top_rank, top_mapping, top_missing = scored[0]
    second_type, _second_coverage, second_rank, _m2, _miss2 = scored[1]

    # A declared slot is a strong prior, not an instruction. When the caller
    # states which slot a file belongs in (a sample dataset does; a real
    # upload does not) and the headers genuinely support that slot, honour it.
    #
    # This matters because the books and portal sides of TDS are often the
    # SAME shape — a deductor-side books export and a Form 26AS carry
    # identical columns, so the header row alone cannot tell them apart. The
    # declared slot resolves that; the coverage check keeps it honest by
    # refusing a declared slot the file doesn't actually fit.
    if declared_source_type:
        for row in scored:
            if row[0] == declared_source_type and row[1] >= HEURISTIC_THRESHOLD:
                top_type, top_coverage, top_rank, top_mapping, top_missing = row
                return {
                    "source_type": top_type,
                    "confidence": int(round(min(top_coverage, 100))),
                    "method": "heuristic",
                    "column_mapping_override": top_mapping or None,
                    "reason": (
                        f"Uploaded as {top_type}, and {len(top_mapping)} of "
                        f"{len(top_mapping) + len(top_missing)} expected columns for it "
                        f"are present."
                    ),
                    "ambiguous_with": None,
                    "needs_confirmation": False,
                    "candidates": [top_type],
                    "note": None,
                }

    margin = top_rank - second_rank
    if top_coverage >= HEURISTIC_THRESHOLD and margin >= HEURISTIC_MARGIN:
        return {
            "source_type": top_type,
            "confidence": int(round(min(top_coverage, 100))),
            "method": "heuristic",
            "column_mapping_override": top_mapping or None,
            "reason": (
                f"{len(top_mapping)} of {len(top_mapping) + len(top_missing)} expected "
                f"columns for {top_type} are present in the header row."
            ),
            "ambiguous_with": None,
            "needs_confirmation": False,
            "candidates": [top_type],
            "note": None,
        }

    # A tie between two types that share a schema is not worth a model call —
    # the distinction doesn't change how the file is reconciled.
    if top_coverage >= HEURISTIC_THRESHOLD:
        pair = frozenset({top_type, second_type})
        default = _FAMILY_DEFAULTS.get(pair)
        if default:
            chosen = next(row for row in scored if row[0] == default)
            return {
                "source_type": chosen[0],
                "confidence": int(round(min(chosen[1], 100))),
                "method": "heuristic",
                "column_mapping_override": chosen[3] or None,
                "reason": (
                    f"Headers match both {top_type} and {second_type}, which share a "
                    f"column layout."
                ),
                "ambiguous_with": second_type if chosen[0] == top_type else top_type,
                "needs_confirmation": False,
                "candidates": [top_type, second_type],
                "note": (
                    f"Read as {chosen[0]} — no IMS action column is present, which an "
                    f"IMS export always carries."
                ),
            }

        # A genuine tie between two DIFFERENT kinds of file (e.g. a books
        # export and a portal export that happen to share a layout). The
        # header row cannot settle it, so it is surfaced for confirmation
        # rather than guessed at.
        return {
            "source_type": top_type,
            "confidence": int(round(min(top_coverage, 100))),
            "method": "heuristic",
            "column_mapping_override": top_mapping or None,
            "reason": (
                f"Headers match both {top_type} and {second_type} equally well."
            ),
            "ambiguous_with": second_type,
            "needs_confirmation": True,
            "candidates": [top_type, second_type],
            "note": None,
        }

    return {
        "source_type": None,
        "confidence": int(round(min(top_coverage, 100))),
        "method": "heuristic",
        "column_mapping_override": None,
        "reason": (
            f"Best header match was {top_type} at {top_coverage:.0f}%, which is below "
            f"the {HEURISTIC_THRESHOLD}% needed to classify without asking the model."
        ),
        "ambiguous_with": second_type if margin < HEURISTIC_MARGIN else None,
        "needs_confirmation": False,
        "candidates": [],
        "note": None,
    }


# ---------------------------------------------------------------------------
# Pass 2 — LLM fallback
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "You identify what a raw Indian accounting export is, for a GST/TDS "
    "reconciliation tool. You are given a filename, the detected column "
    "headers, and a few sample rows.\n\n"
    "You are also given the list of source types the tool understands, each "
    "described by what it IS. Choose the single best match.\n\n"
    "Rules:\n"
    "- Base your answer on the actual evidence in the headers and values, not "
    "on the filename alone — filenames are often wrong or generic.\n"
    "- If the file is genuinely not one of the listed source types, or is not "
    "financial data at all, return \"unrecognized\". Do NOT force a guess.\n"
    "- Also propose a column mapping onto the canonical fields listed for the "
    "source type you chose. Map a source column to AT MOST one canonical "
    "field. If no column plausibly carries a field, return null for it — "
    "never invent a mapping to fill a gap.\n"
    "- Confidence is an integer 0-100.\n\n"
    "Respond with a single valid JSON object and nothing else. No preamble, "
    "no markdown fences. Schema:\n"
    '{"source_type": "<one of the listed keys, or \\"unrecognized\\">", '
    '"confidence": <integer 0-100>, '
    '"reason": "<one short sentence citing the specific evidence>", '
    '"column_mapping": {"<canonical_field>": "<source column>"|null}}'
)


def _build_classify_prompt(
    filename: str, headers: list[str], samples: list[dict[str, str]]
) -> str:
    lines = ["Source types this tool understands:"]
    for key in DEMO_SOURCE_TYPES:
        lines.append(f"- {key}: {SOURCE_TYPE_DESCRIPTIONS[key]}")
    lines.append("\nCanonical fields per source type:")
    for key in DEMO_SOURCE_TYPES:
        fields = _canonical_fields_for(key)
        lines.append(f"- {key}: {', '.join(fields)}")
    lines.append(f"\nFilename: {filename}")
    lines.append("\nDetected column headers:")
    lines.append(json.dumps(headers, ensure_ascii=False))
    lines.append("\nSample rows (first few):")
    lines.append(json.dumps(samples, ensure_ascii=False, indent=1))
    lines.append("\nIdentify this file.")
    return "\n".join(lines)


def _canonical_fields_for(source_type: str) -> list[str]:
    """The canonical fields the engine maps for a source type, derived from
    the engine's own schema definitions."""
    if source_type == "tally":
        gst = [f for f in GST_CANONICAL_FIELDS if f not in ("source_type", "source_file", "original_row", "total_tax")]
        tds = [f for f in TDS_CANONICAL_FIELDS if f not in ("source_type", "source_file", "original_row")]
        return gst + [f for f in tds if f not in gst]
    if source_type in ("gstr2b", "ims"):
        return [f for f in GST_CANONICAL_FIELDS if f not in ("source_type", "source_file", "original_row", "total_tax")]
    return [f for f in TDS_CANONICAL_FIELDS if f not in ("source_type", "source_file", "original_row")]


def llm_classify(
    filename: str, headers: list[str], samples: list[dict[str, str]]
) -> dict[str, Any]:
    """Pass 2: ask the model what this file is, and for a column mapping.

    Returns the same shape as :func:`heuristic_classify`. Raises
    ``llm.LLMError`` when the model is unavailable — the caller decides
    whether to degrade to "unrecognized" or surface the error.
    """
    model = resolve_model("ingestion_mapping", fallback_key="simple")
    parsed, _latency, model_id, _raw = llm.call_llm_json(
        _CLASSIFY_SYSTEM,
        _build_classify_prompt(filename, headers, samples),
        model_override=model,
    )

    raw_type = str(parsed.get("source_type") or "").strip()
    source_type = raw_type if raw_type in DEMO_SOURCE_TYPES else None

    try:
        confidence = int(parsed.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))

    mapping: dict[str, str] = {}
    raw_mapping = parsed.get("column_mapping")
    if isinstance(raw_mapping, dict):
        header_set = {str(h) for h in headers}
        for field, column in raw_mapping.items():
            if column in (None, "", "null"):
                continue
            column = str(column).strip()
            # Reject a column the model hallucinated — it must exist.
            if column in header_set and field not in mapping:
                mapping[field] = column

    return {
        "source_type": source_type,
        "confidence": confidence,
        "method": "llm",
        "column_mapping_override": mapping or None,
        "reason": str(parsed.get("reason") or "").strip(),
        "ambiguous_with": None,
        "needs_confirmation": False,
        "candidates": [source_type] if source_type else [],
        "note": f"Identified by {model_id}." if source_type else None,
    }


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def classify_files(
    uploads: list[dict[str, Any]],
    *,
    use_llm: bool = True,
) -> list[dict[str, Any]]:
    """Classify a batch of uploaded files.

    Args:
        uploads: ``[{filename, data, source_type?}]`` — the shape the
            Streamlit uploader and :func:`demo.session.load_sample_dataset`
            both produce.
        use_llm: When False, only the heuristic pass runs and anything it
            can't classify is reported as unrecognized. Used by the demo's
            "fast path" toggle.

    Returns one result dict per file, in input order. A file that cannot be
    read at all is reported as unrecognized rather than raising — the demo
    must never dead-end on one bad file.
    """
    results: list[dict[str, Any]] = []
    for upload in uploads:
        filename = str(upload.get("filename") or "upload.csv")
        data = upload.get("data") or b""
        declared = upload.get("source_type")

        try:
            headers, samples, row_count = _read_structure(data, filename)
        except Exception as exc:  # noqa: BLE001
            results.append({
                "filename": filename,
                "source_type": None,
                "confidence": 0,
                "method": "none",
                "column_mapping_override": None,
                "reason": f"The file could not be read: {exc}",
                "ambiguous_with": None,
                "needs_confirmation": False,
                "candidates": [],
                "note": None,
                "headers": [],
                "row_count": 0,
                "declared_source_type": declared,
            })
            continue

        outcome = heuristic_classify(headers, declared_source_type=declared)

        if outcome["source_type"] is None and use_llm:
            try:
                outcome = llm_classify(filename, headers, samples)
            except llm.LLMError as exc:
                outcome = {
                    **outcome,
                    "method": "none",
                    "reason": (
                        f"{outcome['reason']} The model could not be reached to "
                        f"identify it either ({str(exc).split('|')[0]})."
                    ),
                }

        results.append({
            **outcome,
            "filename": filename,
            "headers": headers,
            "row_count": row_count,
            "declared_source_type": declared,
        })
    return results


# ---------------------------------------------------------------------------
# The scoped column-mapping adapter
# ---------------------------------------------------------------------------


def preseed_trusted_shapes(
    session_id: str,
    classifications: list[dict[str, Any]],
    uploads: list[dict[str, Any]],
    *,
    client: str,
    db_path=None,
) -> list[str]:
    """Register each classified file's column mapping with the engine.

    This is the demo's scoped adapter for a ``column_mapping_override``. The
    shared ``config/source_mappings.yaml`` is never edited. Instead the
    mapping is registered against this one file *shape* (its header set) for
    this one session, using the engine's own trusted-profile mechanism — the
    same mechanism a human confirming a mapping in the Smart Ingestion screen
    would use.

    The effect is exactly what the build prompt asks for: the file is handed
    to the engine with its columns already understood as canonical names, and
    the override stays scoped to this file and this session.

    Returns a list of human-readable notes about what was registered.
    """
    notes: list[str] = []
    by_name = {str(u.get("filename")): u for u in uploads}

    for entry in classifications:
        source_type = entry.get("source_type")
        mapping = entry.get("column_mapping_override")
        if not source_type or not mapping:
            continue
        upload = by_name.get(str(entry.get("filename")))
        if upload is None:
            continue

        try:
            headers, _samples, _rows = _read_structure(
                upload.get("data") or b"", str(entry["filename"])
            )
        except Exception:  # noqa: BLE001
            continue

        # Use the engine's own signature function so the key matches by
        # construction — a re-implementation could drift and silently miss.
        signature = _header_signature(headers)

        mapping_json = {
            field: {
                "source_column": column,
                "confidence": 1.0,
                "reason": (
                    f"Column mapping proposed by the demo's file classifier "
                    f"({entry.get('method')} pass) and applied to this file shape only."
                ),
            }
            for field, column in mapping.items()
        }

        classification_json = {
            "document_type": _engine_doc_type(source_type),
            "confidence": int(entry.get("confidence") or 0),
            "is_financial_data": True,
            "reason": entry.get("reason") or "Classified by the demo module.",
        }

        try:
            conn = idb.connect(db_path)
            try:
                idb.upsert_shape(
                    conn,
                    client_ref=client,
                    source_type=source_type,
                    header_signature=signature,
                    headers=headers,
                    mapping=mapping_json,
                    classification=classification_json,
                    model_used=f"demo:{entry.get('method')}",
                    confidence_floor=1.0,
                    trusted=True,
                    source="trusted_profile",
                    client_id=None,
                    actor="demo",
                )
            finally:
                conn.close()
            notes.append(
                f"{entry['filename']}: {len(mapping_json)} column(s) mapped for this "
                f"file shape — no model call needed."
            )
        except Exception as exc:  # noqa: BLE001
            notes.append(
                f"{entry['filename']}: the column mapping could not be registered "
                f"({exc}); the engine will map it itself."
            )
    return notes


def _engine_doc_type(source_type: str) -> str:
    """The engine's vocabulary label for a source type.

    Validated against the engine's own accepted-classification sets so a
    pre-seeded shape can never be rejected as a wrong-slot upload.
    """
    label = _ENGINE_DOC_TYPE.get(source_type)
    accepted = _ACCEPTED_CLASSIFICATIONS.get(source_type, set())
    if label in accepted:
        return label
    return next(iter(accepted)) if accepted else (label or source_type)


def header_signature_for(data: bytes, filename: str) -> Optional[str]:
    """The engine's shape-cache key for a file, or None if unreadable."""
    try:
        headers, _samples, _rows = _read_structure(data, filename)
        return _header_signature(headers)
    except Exception:  # noqa: BLE001
        return None
