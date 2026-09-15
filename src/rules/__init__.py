"""C1 — Rules, Taxonomy & Regulatory Configuration package.

The system's editable rulebook: Rules Workspace (five rule categories),
Taxonomy Editor (pre-seeded), Regulatory Rules Table (pre-seeded with
dated rates), effective-dated Rule Versions at BOTH the firm-wide and
per-client-override layers, and a new Statutory Due Dates category for C2.

Sub-modules (mirrors src/auth/, src/clients/, src/settings/):
- schema.py  — SQLite tables, shares db/poc.db.
- db.py      — low-level CRUD.
- seed.py    — pre-seeded taxonomy + regulatory rates + rule categories.
- service.py — the public API every other module should import.

Key design rules from the C1 build prompt:
- Effective-dating at TWO independent layers: a firm-wide default Rule
  Version AND a per-client override Rule Version, each independently
  date-versioned. Deliberate, to avoid a Module 2 retrofit later.
- Rule changes are forward-only: an edit inserts a NEW RuleVersion and
  never rewrites a past version (so past transactions keep their
  classification/calculation).
- Manager-level approval is sufficient for ALL rule types (no separate
  Partner-only tier); changes go live immediately (no dry-run gate —
  that is deferred to Module 9).
- The change log is a LOCAL stub, retrofit to F4 once F4 is built.
"""

from __future__ import annotations