"""Setu demo/sandbox module — a standalone, client-agnostic demo of the
reconciliation engine.

This package lives ALONGSIDE the Phase 1 PoC and imports it as a library.
Nothing under ``src/``, ``config/`` or ``db/`` is modified by anything here.

The four things this module adds (everything else is reuse):

  * ``classify.py``    — file-type detection (heuristic + LLM fallback)
  * ``ai_insights.py`` — Unit 2D, the AI post-matching analysis layer
  * ``report.py``      — single self-contained HTML report
  * ``app.py``         — the Streamlit chat shell

``session.py`` holds the ephemeral session/folder plumbing they share.

Run with::

    streamlit run demo/app.py
"""

from __future__ import annotations

__all__ = ["session", "classify", "ai_insights", "report"]
