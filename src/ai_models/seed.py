"""Seed data for the central AI model registry.

Additive-upsert only — never resets a model an admin changed, mirroring
auth.seed's _sync_new_permissions and rules.seed.

The touchpoint list below is the UNION of every AI touchpoint that exists
across the modules built so far, so the registry is a genuine single view
rather than a new silo:

  * ``ingestion_mapping``      — the unified ingestion layer's column
                                 mapping (src/ingestion_ai/llm.py).
  * ``gst_classification``     — C5's placeholder touchpoint.
  * ``tds_classification``     — Module 2's narration classifier.
  * ``recon_explanation``      — the post-matching analysis layer
                                 (src/ai_analysis.py).
  * ``document_review``        — C5's placeholder touchpoint.
  * ``invoice_extraction_visual`` / ``invoice_extraction_structured`` —
                                 F3-B's two extraction paths.

Default model ids are real OpenRouter ids. Where a module previously used a
human-readable label (F3-B's "Claude Sonnet (vision)"), the seed maps it to
the actual id so the registry is consistent — the label is preserved in the
touchpoint's own ``label`` field.
"""

from __future__ import annotations

import sqlite3

from src.ai_models import db as adb

# (touchpoint_key, label, category, description, primary_model, fallback_model, sort_order)
SEED_ASSIGNMENTS: list[tuple[str, str, str, str, str, str, int]] = [
    (
        "ingestion_mapping",
        "Ingestion — column mapping",
        "Ingestion",
        "Reads uploaded files and maps their columns onto the reconciliation schema. "
        "Feeds the engine directly, so a wrong mapping here corrupts every downstream match.",
        "anthropic/claude-opus-4.1",
        "anthropic/claude-sonnet-4",
        1,
    ),
    (
        "invoice_extraction_visual",
        "Invoice extraction — visual (images / scanned PDFs)",
        "Extraction",
        "Extracts invoice fields from images and scanned PDFs, where the model must read the page.",
        "anthropic/claude-sonnet-4",
        "google/gemini-2.5-pro",
        2,
    ),
    (
        "invoice_extraction_structured",
        "Invoice extraction — structured (native PDF / Excel / Word)",
        "Extraction",
        "Extracts invoice fields from files that already carry a text layer or tabular structure.",
        "deepseek/deepseek-chat",
        "qwen/qwen-2.5-72b-instruct",
        3,
    ),
    (
        "recon_explanation",
        "Reconciliation — exception explanation",
        "Analysis",
        "Explains a reconciliation exception: probable causes, a suggested fix, and reasoning. "
        "Routed per classification — see config/ai_config.yaml.",
        "deepseek/deepseek-r1",
        "qwen/qwen-2.5-72b-instruct",
        4,
    ),
    (
        "tds_classification",
        "TDS — section classification from narration",
        "Analysis",
        "Suggests a TDS section from free-text narration. The rate is always looked up "
        "deterministically from C1 — the model never sets a rate.",
        "deepseek/deepseek-chat",
        "qwen/qwen-2.5-72b-instruct",
        5,
    ),
    (
        "gst_classification",
        "GST — classification (placeholder)",
        "Analysis",
        "Reserved touchpoint from C5's vocabulary. Not yet wired to a live call.",
        "deepseek/deepseek-chat",
        "qwen/qwen-2.5-72b-instruct",
        6,
    ),
    (
        "document_review",
        "Document review (placeholder)",
        "Analysis",
        "Reserved touchpoint from C5's vocabulary. Not yet wired to a live call.",
        "deepseek/deepseek-chat",
        "qwen/qwen-2.5-72b-instruct",
        7,
    ),
    (
        "f6_mapping_proposal",
        "F6 — mapping proposal (unrecognised source layout)",
        "Ingestion",
        "Proposes a field mapping + sheet classification for an unrecognised source file "
        "layout (Path B). Long structured-context reading, strict JSON output, no arithmetic "
        "— the model never computes a value, only proposes which columns mean what. Every "
        "proposal requires human confirmation before a format is promoted.",
        "anthropic/claude-sonnet-4",
        "deepseek/deepseek-chat",
        8,
    ),
]

# The touchpoint whose model the ingestion layer actually reads at runtime.
INGESTION_TOUCHPOINT = "ingestion_mapping"


def run_seed(conn: sqlite3.Connection) -> None:
    """Seed the touchpoint list. Idempotent and additive — an existing row's
    model assignment is never overwritten."""
    for key, label, category, description, primary, fallback, order in SEED_ASSIGNMENTS:
        adb.upsert_assignment(
            conn,
            touchpoint_key=key,
            label=label,
            category=category,
            description=description,
            primary_model=primary,
            fallback_model=fallback,
            is_active=True,
            sort_order=order,
        )
