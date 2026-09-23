"""Step 0 — the Foundation component library (built once).

Modules import their components from here rather than styling anything ad
hoc. See ``components.py`` for the component catalogue and ``tokens.py`` for
the locked design tokens.
"""

from setu.foundation import components, tokens

__all__ = ["components", "tokens"]
