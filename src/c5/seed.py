"""First-run seed data for the C5 AI Instruction & Knowledge Library module.

Per the C5 build prompt: the touchpoint vocabulary is seeded (two live
touchpoints — ingestion_mapping, tds_classification — plus the placeholder
touchpoints from C3-extended, tagged at creation for future use).

Additive-upsert only — never resets an Admin's edits, mirrors auth.seed's
_sync_new_permissions and rules.seed.
"""

from __future__ import annotations

import sqlite3

from src.c5 import db as cdb

# (key, label, is_active, sort_order)
# is_active=1 means the touchpoint is wired and live; is_active=0 is a
# placeholder from C3-extended, tagged for future use but not yet read.
SEED_TOUCHPOINTS: list[tuple[str, str, int, int]] = [
    ("ingestion_mapping", "Ingestion mapping", 1, 1),
    ("tds_classification", "TDS classification", 1, 2),
    # Placeholder touchpoints from C3-extended's routing table (not wired).
    ("gst_classification", "GST classification", 0, 3),
    ("recon_explanation", "Reconciliation explanation", 0, 4),
    ("document_review", "Document review", 0, 5),
]


def run_seed(conn: sqlite3.Connection) -> None:
    for key, label, is_active, sort_order in SEED_TOUCHPOINTS:
        cdb.upsert_touchpoint(conn, key=key, label=label, is_active=bool(is_active), sort_order=sort_order)
    conn.commit()