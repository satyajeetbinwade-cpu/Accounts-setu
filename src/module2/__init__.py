"""Module 2 — Reconciliation Engine (2A GST / 2B TDS / 2C Other).

Production rebuild of the reconciliation core. This package EXTENDS Phase 1's
already-tested matching engines (src/matching/gst_matcher.py Unit 3 and
src/matching/tds_matcher.py Unit 4) rather than rewriting them — see
src/module2/service.py's module docstring for the reuse-first contract.

Layers (mirrors src/rules, src/f5, src/filing):
    schema.py  — SQLite tables (shares db/poc.db)
    db.py      — low-level CRUD
    service.py — PUBLIC API; every other module and every UI file imports
                 from here, never from db.py directly.
"""