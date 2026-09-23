"""Framework-agnostic helpers shared by the Reflex frontend.

These modules are presentation infrastructure with no UI-framework
dependency, so they can be imported freely by any layer:

  * ``design_tokens.py`` — the locked color/typography/layout tokens
    (single source of truth for the design system).
  * ``discovery.py``     — on-disk discovery of clients, periods, recon
    types and source files under ``data/``.

Nothing here talks to Reflex or any other UI framework on import.
"""
