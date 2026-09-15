"""F3-B — Invoice Extraction & Digitalization (standalone module).

A self-contained, AI-assisted invoice digitization pipeline: upload
invoices in any of four common formats, extract a fixed canonical field
set through a configurable AI layer with per-field confidence scoring,
gate anything under a 90% confidence threshold into this module's OWN
manual review queue, and export a Tally-import-ready Excel/CSV batch.

Per the F3-B build prompt this module is a SCOPE ADDITION beyond the
locked 17-module catalogue, built and shipped as a fully separate,
standalone module: it does NOT retrofit F3's Unified Review Queue or
Module 2's matching workflow in this build. Both integration points are
provisioned structurally (see service.py's Cross-Module Retrofit Flags)
but intentionally left unwired.

Layering mirrors every other module in this repo:
  * schema.py  — SQLite tables (shares db/poc.db)
  * seed.py    — canonical field set + the two C3-ext touchpoint rows
  * extractor.py — the AI extraction stand-in (visual vs structured paths)
  * db.py      — low-level CRUD
  * service.py — PUBLIC API (everything else imports from here)
"""
