"""F6 — Source File Ingestion & Format Registry.

Scope addition beyond the locked 17-module catalogue (see the F6 build
prompt). Sits in the Foundation layer: every ingesting module depends on
it, and it owns no domain logic of its own. Replaces Phase 1's hardcoded
ingestion unit outright (that path assumed one fixed schema); Module 2
consumes this module's normalized output unchanged.
"""
