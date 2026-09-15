"""F3-AI — Smart Document Ingestion.

An AI-assisted layer in front of F3 (src/documents/) that reads an
uploaded raw source file, infers a column-to-canonical-field mapping,
and either applies it automatically at high confidence or routes it to
a person via F3's shared Unified Review Queue (tagged "couldn't map").

Depends on: F3 (repository + queue), C1's canonical schema (adapted —
see service.py docstring), C3-extended (model routing — first active
touchpoint, stubbed until C3-ext is built), C5 (instruction library —
first active touchpoint, genuinely live).
"""
