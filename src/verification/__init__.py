"""Verification harness — measures engine accuracy against a manual baseline.

No matching, ingestion or export logic lives here. This package reads engine
results from the DB (via src/queries) and a manual baseline from an Excel
file, aligns them, and reports agreement metrics.

See verify.py for the CLI entry point.
"""
