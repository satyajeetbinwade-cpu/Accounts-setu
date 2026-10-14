"""Seed a SCRATCH database with a demo upload the review screen can be seen on.

    python -m src.invoice_extract.demo_seed --db <path>

Why this exists: the v2 review screen cannot be looked at without a triaged
upload, and a real one costs money in model calls and needs API keys. This
builds the same rows the pipeline would produce, from the EXPECTED-FIELDS
FIXTURE, with NO model call at all:

* one demo client and one upload;
* a triage run with per-page rows and documents made of page regions — including
  a page holding several regions and a document spanning two pages;
* one extraction per document with the fixture's values (printed values from
  BOTH reads, plus any handwritten annotations);
* the deterministic layer run for real (``rules.run_rules``) so checks,
  applicability, derived values and scores are the app's own, then three
  deliberate demo states: one required field MISSING, one check FAILED, and two
  direction decisions (one likely-purchase, one "can't tell").
* a page image source: the real bundle from ``SETU_TRIAGE_FIXTURE_PDF`` when it
  is set, otherwise a small placeholder PDF with numbered pages.

SAFETY: it refuses to touch the app's real database, and it only ever INSERTs.
Everything it makes is business data already committed in the fixture (company
names, GSTINs, amounts) — no personal names, addresses or phone numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from src import db as recon_db
from src import bootstrap
from src.invoice_extract import doc_actions
from src.invoice_extract import extract as extract_mod
from src.invoice_extract import fields_v2 as fv2
from src.invoice_extract import normalisers as nz
from src.invoice_extract.service import InvoiceExtractError, _connect, _now

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_FIELDS = (
    REPO_ROOT / "tests" / "fixtures" / "invoice_extract" / "batch_apr2026_expected_fields.json"
)

DEMO_CLIENT_NAME = "Demo Textiles Private Limited"
DEMO_CLIENT_PAN = "AAACD1234E"
DEMO_CLIENT_GSTIN = "07AAECK9054D1ZK"          # the bundle's own issuing GSTIN
DEMO_FILENAME = "batch_apr2026_demo.pdf"
DEMO_PLACEHOLDER_PAGES = 12

#: Which fixture documents to build, and the demo state each one carries.
#: ``missing`` names a key that is deliberately NOT read (a required field the
#: reviewer must deal with); ``failed_check`` forces a check to fail.
DEMO_DOCUMENTS: tuple[dict[str, Any], ...] = (
    {
        "label": "DEMO/2026-27/001 — two regions on one page",
        "fixture": "p4",
        "category": "tax_invoice",
        "regions": [(2, 0.06, 0.05, 0.40, 0.35), (2, 0.52, 0.05, 0.42, 0.35)],
        "failed_check": "totals_reconcile",
        "direction": "likely_purchase",
    },
    {
        "label": "DEMO/2026-27/002 — a missing required field",
        "fixture": "p2",
        "category": "tax_invoice",
        "regions": [(3, 0.06, 0.05, 0.88, 0.45)],
        "missing": "invoice_total",
        "direction": "purchase",
    },
    {
        "label": "DEMO/2026-27/003 — one page, clean",
        "fixture": "p3",
        "category": "tax_invoice",
        "regions": [(4, 0.06, 0.05, 0.88, 0.45)],
        "direction": "purchase",
        "confirmed_direction": True,
    },
    {
        "label": "DEMO/airtel — ONE document across TWO pages",
        "fixture": "p10-p11",
        "category": "tax_invoice",
        "regions": [(10, 0.07, 0.30, 0.55, 0.40), (11, 0.07, 0.10, 0.88, 0.45)],
    },
    {
        "label": "DEMO/no-GST — no tax at all",
        "fixture": "p23",
        "category": "no_gst_expense",
        "regions": [(23, 0.06, 0.05, 0.88, 0.45)],
        "direction": "undetermined",
    },
    {
        "label": "DEMO/handwriting — a figure written over a printed one",
        "fixture": "p24",
        "category": "tax_invoice",
        "regions": [(24, 0.06, 0.05, 0.88, 0.45)],
    },
    {
        "label": "DEMO/supporting — not extracted",
        "fixture": "p25",
        "category": "supporting",
        "regions": [(25, 0.06, 0.05, 0.88, 0.45)],
    },
    {
        # Excluded by its category, so the drawer and the progress counter both
        # show that state. Never extracted.
        "label": "DEMO/docket — excluded (not a voucher)",
        "fixture": None,
        "category": "not_a_voucher",
        "regions": [(26, 0.06, 0.05, 0.88, 0.45)],
    },
)

#: Structural fields the fixture does not assert (it asserts the VALUES under
#: review). A demo document needs them filled or every document would show a
#: pile of missing-required items and the states this seed exists to show would
#: be buried.
_TOPUP_TEXT: dict[str, str] = {
    "issuer_name": "Demo Supplies Private Limited",
    "issuer_gstin": DEMO_CLIENT_GSTIN,
    "recipient_name": DEMO_CLIENT_NAME,
    "recipient_gstin": DEMO_CLIENT_GSTIN,
    # Deliberately NOT the issuer's own state (the issuing GSTIN is 07, Delhi):
    # these are inter-state documents, so IGST is the right tax head and a
    # Delhi-to-Delhi top-up would invent a CGST/SGST pair the fixture denies.
    "place_of_supply": "Uttar Pradesh",
}


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _refuse_the_real_database(db_path: str) -> Path:
    """A demo seed must never land in the database the app runs on.

    The database to protect is the REPOSITORY's own (``REPO_DB_PATH``), not
    ``DB_PATH``: when ``SETU_DB_PATH`` points the app at a scratch copy, that
    copy IS the one this command is meant to seed.

    A path that does not exist yet is allowed: creating a fresh scratch database
    is one of the two things this command is for (the other is seeding a copy).
    """
    target = Path(db_path).expanduser().resolve()
    real = Path(recon_db.REPO_DB_PATH).expanduser().resolve()
    if target == real:
        raise InvoiceExtractError(
            f"{target} is the repository's real database. Pass a COPY or a scratch "
            "database (--db /tmp/demo.db) — this command only ever writes demo rows."
        )
    return target


def _page_source(pages: int) -> bytes:
    """The real bundle when the environment provides one, else placeholders."""
    env = os.environ.get("SETU_TRIAGE_FIXTURE_PDF")
    if env and Path(env).exists():
        return Path(env).read_bytes()
    import fitz

    doc = fitz.open()
    for number in range(1, pages + 1):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 70), f"DEMO PAGE {number}", fontsize=20)
        page.insert_text((60, 110), "placeholder page — set SETU_TRIAGE_FIXTURE_PDF", fontsize=11)
        page.draw_rect(fitz.Rect(50, 50, 545, 200), fill=(0.12, 0.12, 0.12))
    data = doc.tobytes()
    doc.close()
    return data


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------


def seed(db_path: str, *, actor: str = "demo:seed", client_id: Optional[int] = None) -> dict[str, Any]:
    """Build the demo upload. Returns a summary of what it made."""
    target = _refuse_the_real_database(db_path)
    fixture = json.loads(EXPECTED_FIELDS.read_text())
    by_tag = {d["label"].split(" ")[0]: d for d in fixture.get("documents", [])}

    # A scratch database may never have been booted; migrate + seed it the same
    # way the app does (idempotent), scoped to its own folder so nothing in the
    # repository's data/ tree can be touched.
    bootstrap.run_migrations(db_path=str(target), data_root=target.parent / "data")

    from src.ai_models import service as ai_models
    from src.invoice_extract import rules as rules_mod
    from src.invoice_extract.service import get_threshold
    from src.settings import service as settings

    # Both flags ON, in THIS database only: the point of the demo is to see the
    # v2 flow, and a scratch database is not the app's.
    settings.set_setting_str(extract_mod.KEY_ENABLED, "true", actor=actor, db_path=str(target))
    from src.invoice_extract import triage as triage_mod

    settings.set_setting_str(triage_mod.KEY_ENABLED, "true", actor=actor, db_path=str(target))

    pages = max(page for doc in DEMO_DOCUMENTS for page, *_ in doc["regions"])
    file_bytes = _page_source(pages)

    conn = _connect(str(target))
    try:
        cid = int(client_id or _ensure_demo_client(conn, actor=actor, db_path=str(target)))
        upload_id, run_id = _make_triage(conn, cid, file_bytes, pages, actor=actor)
        made = _make_documents(conn, upload_id, run_id, by_tag, actor=actor)
        conn.commit()
    finally:
        conn.close()

    # The deterministic layer, for real: checks, applicability, derived values
    # and the Phase-4 scores all come out of the app's own code.
    for row in made:
        if row.get("extraction_id"):
            rules_mod.run_rules(row["extraction_id"], actor=actor, db_path=str(target))
    _apply_demo_states(str(target), made, actor=actor)

    return {
        "db": str(target),
        "client_id": cid,
        "upload_id": upload_id,
        "run_id": run_id,
        "documents": [
            {"document_id": r["document_id"], "ordinal": r["ordinal"],
             "category": r["category"], "label": r["label"], "pages": r["pages"]}
            for r in made
        ],
        "page_image_source": "SETU_TRIAGE_FIXTURE_PDF" if os.environ.get("SETU_TRIAGE_FIXTURE_PDF") else "placeholders",
        "threshold": get_threshold(db_path=str(target)),
        "assigned_models": {
            key: (lambda a: {"primary": (a or {}).get("primary_model"),
                             "fallback": (a or {}).get("fallback_model")})(
                ai_models.get_assignment(key, db_path=str(target)))
            for key in ("invoice_triage", "invoice_extraction_visual", "invoice_extraction_verify")
        },
        "note": (
            "the three vision touchpoints start UNASSIGNED; the demo needs no model call, "
            "so the screen works as-is. Assign models on the AI Models screen to run the "
            "real pipeline."
        ),
    }


def _ensure_demo_client(conn, *, actor: str, db_path: str) -> int:
    row = conn.execute(
        "SELECT client_id FROM end_clients WHERE legal_name = ?", (DEMO_CLIENT_NAME,),
    ).fetchone()
    if row is not None:
        return int(row[0])
    from src.clients.service import create_client

    return create_client(
        legal_name=DEMO_CLIENT_NAME, pan=DEMO_CLIENT_PAN, gstin=DEMO_CLIENT_GSTIN,
        actor=actor, db_path=db_path,
    )


def _make_triage(conn, client_id: int, file_bytes: bytes, pages: int, *, actor: str) -> tuple[int, int]:
    """The upload, its triage run, the page rows and the document regions."""
    from src.invoice_extract import db as idb

    upload_id = idb.create_upload(
        conn, client_id=client_id, filename=DEMO_FILENAME, file_ext="pdf",
        source_format="pdf", extraction_path="structured", batch_id=None,
        status="extracted", duplicate_warning=False, actor=actor, file_bytes=file_bytes,
        extraction_touchpoint=extract_mod.TOUCHPOINT_READ_A,
        pages_examined=pages, pages_total=pages,
    )
    conn.execute(
        "INSERT INTO invoice_triage_runs (upload_id, status, pages_total, pages_ok, pages_failed, "
        "provider, model, started_at, finished_at, fallback_pages) "
        "VALUES (?, 'complete', ?, ?, 0, 'demo', 'demo-seed', ?, ?, 0)",
        (upload_id, pages, pages, _now(), _now()),
    )
    run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    for page in range(1, pages + 1):
        conn.execute(
            "INSERT INTO invoice_triage_pages (run_id, page, status, rotation_deg, is_blank, "
            "text_layer_chars, attempts, model, fallback_used) "
            "VALUES (?, ?, 'ok', 0, 0, 120, 1, 'demo-seed', 0)",
            (run_id, page),
        )
    return upload_id, run_id


def _make_documents(conn, upload_id: int, run_id: int, by_tag: dict, *, actor: str) -> list[dict[str, Any]]:
    made: list[dict[str, Any]] = []
    for ordinal, spec in enumerate(DEMO_DOCUMENTS, start=1):
        source = by_tag.get(spec["fixture"]) or {}
        excluded = 1 if spec["category"] == doc_actions.DEFAULT_EXCLUDED_CATEGORY else 0
        conn.execute(
            "INSERT INTO invoice_documents (upload_id, run_id, ordinal, category, category_source, "
            "label, excluded, excluded_reason, flags_json, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 'triage', ?, ?, NULL, '[]', 1, ?)",
            (upload_id, run_id, ordinal, spec["category"], spec["label"], excluded, _now()),
        )
        document_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for order, (page, x, y, w, h) in enumerate(spec["regions"]):
            conn.execute(
                "INSERT INTO document_regions (document_id, page, x, y, w, h, order_in_doc, "
                "source, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, 'triage', 1)",
                (document_id, page, x, y, w, h, order),
            )
        pages = sorted({page for page, *_ in spec["regions"]})

        extraction_id = None
        if spec["category"] in extract_mod.TARGET_CATEGORIES:
            extraction_id = extract_mod._insert_extraction(
                conn, document_id=document_id, status="complete",
            )
            extract_mod._update_extraction(
                conn, extraction_id, provider_a="demo", model_a="demo-seed",
                provider_b="demo-verify", model_b="demo-seed", attempts=2,
                finished_at=_now(), input_tokens=0, output_tokens=0, cost_usd=0.0,
            )
            _fill_fields(conn, extraction_id, document_id, spec, source, pages, ordinal=ordinal)
            handwritten = list(source.get("handwritten_amounts") or [])
            if handwritten:
                # The page has a figure written by hand. In the real case it is
                # written OVER the printed value, so the value's box and the
                # annotation's box coincide — that is what makes the guard (and
                # the screen's re-derivation of it) fire. NOTE: the guard's own
                # `guard_hit` is NOT persisted (invoice_doc_fields has no such
                # column); the annotation is, and that is what the screen shows.
                page, rx_, ry, rw, rh = spec["regions"][0]
                ann_box = (rx_ + rw * 0.10, ry + rh * 0.45, rw * 0.30, rh * 0.06)
                conn.execute(
                    "UPDATE invoice_doc_fields SET status = ?, page = ?, "
                    "x = ?, y = ?, w = ?, h = ?, in_region = 1 "
                    "WHERE extraction_id = ? AND field_key = 'line.1.amount'",
                    (fv2.STATUS_NEEDS_PRINTED, page, ann_box[0], ann_box[1],
                     ann_box[2], ann_box[3], extraction_id),
                )
                for annotation in handwritten:
                    extract_mod._insert_annotation(
                        conn, extraction_id=extraction_id, document_id=document_id,
                        ann={"text": annotation, "kind_guess": "payment", "page": page,
                             "x": ann_box[0], "y": ann_box[1], "w": ann_box[2],
                             "h": ann_box[3]},
                    )
        made.append({
            "document_id": document_id, "ordinal": ordinal, "category": spec["category"],
            "label": spec["label"], "pages": pages, "extraction_id": extraction_id,
            "direction": spec.get("direction"), "failed_check": spec.get("failed_check"),
            "confirmed_direction": bool(spec.get("confirmed_direction")),
        })
    return made


def _fill_fields(conn, extraction_id: int, document_id: int, spec: dict, source: dict,
                 pages: list[int], *, ordinal: int) -> None:
    """The fixture's expected values, as if BOTH reads had returned them.

    Plus the structural fields the fixture does not assert (see
    :data:`_TOPUP_TEXT`), so the only things needing a person are the ones this
    seed deliberately creates.
    """
    expected = dict(source.get("expect") or {})
    expected.pop(spec.get("missing") or "", None)
    not_printed = list(source.get("not_printed") or [])
    if spec.get("missing"):
        not_printed.append(spec["missing"])

    topup = dict(_TOPUP_TEXT)
    topup["invoice_number"] = f"DEMO/2026-27/{ordinal:03d}"
    topup["invoice_date"] = "25-Mar-26"
    if spec["category"] == "no_gst_expense":
        # This document prints no GSTIN at all; filling one would contradict the
        # fixture and flip a correct not-printed into a wrong value.
        for key in ("issuer_gstin", "recipient_gstin"):
            topup.pop(key, None)
    for key, value in topup.items():
        if key not in expected and key not in not_printed:
            expected[key] = value

    first_page = pages[0]
    for key in list(fv2.INVOICE_KEYS) + list(expected) + not_printed:
        field_spec = fv2.field_spec(key)
        if field_spec is None:
            continue
        value = expected.get(key)
        if value is None and key not in not_printed:
            continue
        extract_mod._insert_field(
            conn, extraction_id=extraction_id, document_id=document_id,
            row={
                "field_key": key,
                "status": fv2.STATUS_READ if value is not None else fv2.STATUS_NOT_PRINTED,
                "value_a": value, "value_b": value,
                "reads_agree": nz.AGREE_EXACT if value is not None else nz.AGREE_NULL,
                "text_layer_match": 1 if value is not None else None,
                "is_handwritten": 0,
                "page": first_page if value is not None else None,
                "x": 0.10 if value is not None else None, "y": 0.12 if value is not None else None,
                "w": 0.30 if value is not None else None, "h": 0.04 if value is not None else None,
                "in_region": 1 if value is not None else 0,
                "guard_hit": None,
            },
        )


def _apply_demo_states(db_path: str, made: list[dict[str, Any]], *, actor: str) -> None:
    """The three deliberate states, written after the real rules have run."""
    conn = _connect(db_path)
    try:
        for row in made:
            if row.get("direction"):
                direction = row["direction"]
                basis = (
                    "demo seed: the counterparty name matches the client's own name"
                    if direction.startswith("likely")
                    else "demo seed: set for the review screen"
                )
                conn.execute(
                    "UPDATE invoice_doc_direction SET direction = ?, basis = ?, "
                    "confirmed_by = ?, confirmed_at = ? WHERE document_id = ?",
                    (direction, basis,
                     actor if row.get("confirmed_direction") else None,
                     _now() if row.get("confirmed_direction") else None,
                     row["document_id"]),
                )
            if row.get("failed_check") and row.get("extraction_id"):
                conn.execute(
                    "UPDATE invoice_doc_checks SET status = 'fail', detail = ? "
                    "WHERE extraction_id = ? AND check_key = ?",
                    ("demo seed: forced for the review screen", row["extraction_id"],
                     row["failed_check"]),
                )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.invoice_extract.demo_seed",
        description="Seed a SCRATCH database with a demo upload for the review screen.",
    )
    parser.add_argument("--db", required=True, help="a COPY or scratch database (never the real one)")
    parser.add_argument("--client-id", type=int, default=None)
    parser.add_argument("--actor", default="demo:seed")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        summary = seed(args.db, actor=args.actor, client_id=args.client_id)
    except InvoiceExtractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
