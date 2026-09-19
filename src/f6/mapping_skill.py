"""F6 §6 — Mapping Proposal Skill (the ONE AI touchpoint).

A versioned, stored instruction set the AI follows, calling tools for
anything mechanical, returning a structured artifact — never free prose,
never a number. Routed through the SAME LLM client the rest of the app
uses (src/ingestion_ai/llm.py) rather than a new provider integration, and
registered as its own touchpoint in the central AI model registry
(src/ai_models) so it can be assigned/monitored like every other
touchpoint — the closest available stand-in for C3-ext, which this repo
hasn't built yet (per repo convention: same-shape stub, swapped later).

Hard constraints enforced HERE, not just documented (§6):
  - The model NEVER computes/sums/converts/re-types a value. It proposes
    WHICH columns mean what and WHICH must be summed; the deterministic
    parser (parser.py) does all arithmetic from the confirmed config.
  - Confidence is advisory only — `require_human_confirmation` is always
    True; nothing here can auto-promote.
  - The skill body is versioned (SKILL_VERSION) and recorded on every
    MappingProposal so a past decision can be replayed against the
    instruction set that produced it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from src.ingestion_ai import llm

SKILL_VERSION = "f6-mapping-proposal-v1"

TOUCHPOINT_KEY = "f6_mapping_proposal"

_SYSTEM_PROMPT = """You are the Mapping Proposal Skill for Setu's Source File Ingestion module (F6).

You are given a structural digest of an unrecognised source file (sheet names, \
header candidates, sample rows, column dtypes) — never the whole raw file — \
plus the canonical field list for the declared slot (portal or books side).

Your ONLY job: propose which source column(s) map to which canonical field, \
and which sheets are line items vs summary/out-of-scope. You NEVER compute, \
sum, convert, or re-type a single value — you only say WHICH columns mean \
what and WHICH must be summed by the parser afterwards. You never make a \
reconciliation judgement or write exception commentary.

Return STRICT JSON matching this shape exactly, no prose outside the JSON:
{
  "field_mappings": [
    {"canonical_field": "...", "kind": "direct"|"aggregate"|"derived"|"unavailable",
     "source_columns": ["..."], "transform": "..."|null,
     "confidence": 0-100, "rationale": "one line, plain language"}
  ],
  "sheet_classification": [
    {"sheet": "...", "role": "line_items"|"summary_ignored"|"out_of_scope"|"unrecognised"}
  ],
  "row_exclusion_proposals": [
    {"sheet": "...", "predicate": "plain language description"}
  ],
  "ambiguities": ["one question per item, addressed to the human reviewer"],
  "overall_confidence": 0-100
}

An empty ambiguities list on a genuinely unfamiliar layout is suspicious — \
if you are not confident, say so."""


@dataclass
class MappingProposalResult:
    field_mappings: list[dict[str, Any]] = field(default_factory=list)
    sheet_classification: list[dict[str, Any]] = field(default_factory=list)
    row_exclusion_proposals: list[dict[str, Any]] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    overall_confidence: Optional[int] = None
    model_used: str = ""
    skill_version: str = SKILL_VERSION
    require_human_confirmation: bool = True  # ALWAYS True — never a gate the model can pass alone

    def to_json(self) -> dict[str, Any]:
        return {
            "field_mappings": self.field_mappings,
            "sheet_classification": self.sheet_classification,
            "row_exclusion_proposals": self.row_exclusion_proposals,
            "ambiguities": self.ambiguities,
            "overall_confidence": self.overall_confidence,
            "model_used": self.model_used,
            "skill_version": self.skill_version,
        }


def build_structural_digest(
    sheet_names: list[str], sample_rows_by_sheet: dict[str, list[dict[str, Any]]],
    header_candidates: dict[str, Any], closest_version_diff: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """The deterministic-tool-produced input to the skill — NEVER the raw
    file itself. Built by parser.py/fingerprint.py, never by the model
    reading raw bytes."""
    return {
        "sheet_names": sheet_names,
        "header_candidates": header_candidates,
        "sample_rows_by_sheet": {
            name: rows[:5] for name, rows in sample_rows_by_sheet.items()
        },
        "closest_registered_version_diff": closest_version_diff,
    }


def propose_mapping(
    *, slot: str, canonical_fields: list[dict[str, Any]], structural_digest: dict[str, Any],
    db_path=None,
) -> MappingProposalResult:
    """Calls the model once, validates the response against the strict JSON
    contract, and returns a MappingProposalResult. Raises llm.LLMError on
    any provider failure — the caller (service.py) is responsible for
    turning that into a Path B "AI unavailable, map manually" state rather
    than ever inventing a mapping."""
    user_prompt = json.dumps(
        {
            "slot": slot,
            "canonical_fields": canonical_fields,
            "structural_digest": structural_digest,
        },
        default=str,
    )
    parsed, _latency_ms, model_id, _raw = llm.call_llm_json(_SYSTEM_PROMPT, user_prompt, db_path=db_path)
    return _validate_and_build(parsed, model_id)


def _validate_and_build(parsed: dict[str, Any], model_id: str) -> MappingProposalResult:
    """Schema-validate before use (§6's explicit requirement) — malformed
    or missing keys degrade to empty lists rather than raising, so a
    partially-well-formed response still gives the reviewer something to
    work with instead of nothing."""
    field_mappings = parsed.get("field_mappings") or []
    clean_mappings = []
    for fm in field_mappings if isinstance(field_mappings, list) else []:
        if not isinstance(fm, dict) or "canonical_field" not in fm:
            continue
        kind = fm.get("kind")
        if kind not in ("direct", "aggregate", "derived", "unavailable"):
            kind = "unavailable"
        confidence = fm.get("confidence")
        try:
            confidence = max(0, min(100, int(confidence)))
        except (TypeError, ValueError):
            confidence = None
        clean_mappings.append({
            "canonical_field": fm["canonical_field"],
            "kind": kind,
            "source_columns": fm.get("source_columns") or [],
            "transform": fm.get("transform"),
            "confidence": confidence,
            "rationale": str(fm.get("rationale") or ""),
        })

    sheet_classification = [
        s for s in (parsed.get("sheet_classification") or [])
        if isinstance(s, dict) and "sheet" in s and s.get("role") in
        ("line_items", "summary_ignored", "out_of_scope", "unrecognised")
    ]
    row_exclusion_proposals = [
        r for r in (parsed.get("row_exclusion_proposals") or []) if isinstance(r, dict)
    ]
    ambiguities = [str(a) for a in (parsed.get("ambiguities") or []) if a]

    overall_confidence = parsed.get("overall_confidence")
    try:
        overall_confidence = max(0, min(100, int(overall_confidence)))
    except (TypeError, ValueError):
        overall_confidence = None

    return MappingProposalResult(
        field_mappings=clean_mappings,
        sheet_classification=sheet_classification,
        row_exclusion_proposals=row_exclusion_proposals,
        ambiguities=ambiguities,
        overall_confidence=overall_confidence,
        model_used=model_id,
    )
