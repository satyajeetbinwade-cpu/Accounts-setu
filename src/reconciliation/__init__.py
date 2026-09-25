"""Reconciliation Review — presentation-layer derivation (Module 2 upgrade).

Read-only. Everything here is derived from existing ``match_results`` records
plus the C1 rules that were already used by the engine. No matching logic,
classification, confidence scoring, data model or review-state semantic lives
here — this package only *explains* what the engine already decided.
"""

from src.reconciliation import review  # noqa: F401
from src.reconciliation import narrative  # noqa: F401

__all__ = ["review", "narrative"]
