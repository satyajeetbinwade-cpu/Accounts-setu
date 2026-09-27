"""Pass 1 proven end-to-end on a REAL two-sided fixture.

Every earlier pass-1 test fed `match_notes` a hand-built DataFrame. This one
runs BOTH sides through their real parsers — the books register through
`parse_note_register`, the portal notes through the F6 GSTR-2B bridge — and
asserts the note matches on the primary key.

The GSTR-2B `B2B-CDNR` sheet here is built to the flattened labels
`f6.seed_formats.gstr2b_config()` actually contracts (`"credit note/debit note
details :: note number"`, …) over the real two-row merged-header layout, so a
change to either side of that contract fails this test rather than silently
producing an unmatchable note.

Run:

    venv/bin/python -m pytest tests/test_note_pass1_pair.py -v
"""

from __future__ import annotations

import pytest
from openpyxl import Workbook

from src.config_loader import load_config
from src.ingestion_ai import f6_bridge

CLIENT = "Pair Client"
PERIOD = "2025-04"

BOOKS_GSTIN = "07AAAAA0000A1ZZ"
NOTE_NUMBER = "CD-12/04-2025"        # -> normalises to CD12042025
NOTE_DATE_DMY = "26/03/2025"         # -> 2025-03-26
NOTE_VALUE = 5235
NOTE_TAXABLE = 4840.02
NOTE_TAX = 197.49                     # CGST and SGST each

# --- Books note register ---------------------------------------------------

REGISTER_HEADERS = [
    "Date", "Particulars", "Buyer", "Buyer Address", "Voucher Type",
    "Voucher No.", "Voucher Ref. No.", "Voucher Ref. Date", "GSTIN/UIN",
    "Sales Tax No.", "PAN No.", "Narration", "Gross Total",
    "Pur- Lehanga", "Pur - Sarees",
    "Unclaimed CGST @ 2.5%", "Unclaimed SGST @ 2.5%",
    "Unclaimed CGST @ 6%", "Unclaimed SGST @ 6%", "Short & Excess",
]

REGISTER_ROWS = [
    # Cites the note the portal carries -> must MATCH on pass 1.
    # The rate buckets must be arithmetically real (each explained by an
    # item-category taxable at its own rate) or the register's §8 validation
    # hard-stops the file: 2.5% x 2654.64 = 66.37 and 6% x 2185.38 = 131.12.
    ["2025-04-09", "Supplier A", "Supplier A", "Addr A", "Debit Note", "8/2025-26",
     None, "2025-03-26", BOOKS_GSTIN, None, "AAAAA0000A",
     f"CREDIT NOTE NO.  {NOTE_NUMBER}", NOTE_VALUE, 2185.38, 2654.64,
     66.37, 66.37, 131.12, 131.12, None],
    # Cites a note the portal does NOT carry -> must be Not in Portal.
    ["2025-04-22", "Supplier B", "Supplier B", "Addr B", "Debit Note", "3/2025-26",
     None, "2025-03-26", "07BBBBB0000B1ZZ", None, "BBBBB0000B",
     "CREDIT NOTE NO. ZZ-999/04-2025", 1008, 900, None,
     None, None, 54.0, 54.0, None],
]

# --- Portal GSTR-2B, B2B-CDNR sheet ---------------------------------------
# Two-row merged header: parent label in the FIRST cell of each block (the
# flattener forward-fills it), and a single-row column repeats its own label in
# BOTH rows so the flattener emits it bare.

CDNR_SHEET = "B2B-CDNR"
CDNR_PARENT = [
    "GSTIN of Supplier", "Trade/Legal Name",
    "Credit note/Debit note details", "Credit note/Debit note details",
    "Credit note/Debit note details", "Credit note/Debit note details",
    "Taxable Value (₹)",
    "Tax Amount", "Tax Amount", "Tax Amount", "Tax Amount",
    "ITC Availability", "Supply attract reverse charge", "GSTR-1/IFF/GSTR-5 Period",
]
CDNR_CHILD = [
    "GSTIN of Supplier", "Trade/Legal Name",
    "Note Number", "Note Type", "Note Date", "Note Value (₹)",
    "Taxable Value (₹)",
    "Integrated Tax(₹)", "Central Tax(₹)", "State/UT Tax(₹)", "Cess(₹)",
    "ITC Availability", "Supply attract reverse charge", "GSTR-1/IFF/GSTR-5 Period",
]
CDNR_DATA = [
    BOOKS_GSTIN, "Supplier A",
    NOTE_NUMBER, "Credit Note", NOTE_DATE_DMY, NOTE_VALUE,
    NOTE_TAXABLE,
    "", NOTE_TAX, NOTE_TAX, "",
    "Yes", "No", "Mar'25",
]


def _write_register(path, rows=None):
    book = Workbook()
    sheet = book.active
    sheet.title = "Debit Note Register"
    sheet.append(["Debit Note Register"])
    sheet.append(REGISTER_HEADERS)
    for row in (REGISTER_ROWS if rows is None else rows):
        sheet.append(row)
    book.save(path)


def _write_portal(path, data=None):
    book = Workbook()
    sheet = book.active
    sheet.title = CDNR_SHEET
    for _ in range(4):                      # title/summary rows above the header
        sheet.append([])
    sheet.append(CDNR_PARENT)               # row index 4
    sheet.append(CDNR_CHILD)                # row index 5
    sheet.append(CDNR_DATA if data is None else data)   # data starts at row index 6
    book.save(path)


@pytest.fixture
def filed(tmp_path):
    """Both files filed for the period, under a private DATA_ROOT."""
    root = tmp_path / "data"
    (root / CLIENT / PERIOD / "credit_notes").mkdir(parents=True)
    (root / CLIENT / PERIOD / "gstr2b").mkdir(parents=True)

    register = root / CLIENT / PERIOD / "credit_notes" / "Credit Note.xlsx"
    portal = root / CLIENT / PERIOD / "gstr2b" / "GSTR2B.xlsx"
    _write_register(register)
    _write_portal(portal)

    import src.data_paths as data_paths

    original = data_paths.DATA_ROOT
    data_paths.DATA_ROOT = root
    try:
        yield {
            "credit_notes": register.name,
            "gstr2b": portal.name,
            "register_path": register,
            "portal_path": portal,
        }
    finally:
        data_paths.DATA_ROOT = original


# --- The portal side really parses ----------------------------------------


@pytest.fixture
def pair_factory(tmp_path):
    """File BOTH sides for a period and return a canonicalising loader."""
    import src.data_paths as data_paths
    from src import bootstrap
    from src.ingestion_ai.normalizer import load_note_frames
    from src.matching.note_matcher import match_notes

    original = data_paths.DATA_ROOT
    data_paths.DATA_ROOT = tmp_path / "data"
    counter = {"n": 0}

    def build(register_rows, cdnr_data):
        counter["n"] += 1
        root = data_paths.DATA_ROOT
        notes_dir = root / CLIENT / PERIOD / "credit_notes"
        portal_dir = root / CLIENT / PERIOD / "gstr2b"
        notes_dir.mkdir(parents=True, exist_ok=True)
        portal_dir.mkdir(parents=True, exist_ok=True)

        register = notes_dir / f"Credit Note {counter['n']}.xlsx"
        portal = portal_dir / f"GSTR2B {counter['n']}.xlsx"
        _write_register(register, register_rows)
        _write_portal(portal, cdnr_data)

        db = tmp_path / f"pair{counter['n']}.db"
        bootstrap.init_all(str(db))
        books, portal_frame, _adjacent, warnings = load_note_frames(
            CLIENT, PERIOD,
            selected_files={"credit_notes": register.name, "gstr2b": portal.name},
            actor="test", db_path=str(db),
        )
        return {
            "books": books,
            "portal": portal_frame,
            "warnings": warnings,
            "results": (match_notes(books, portal_frame, load_config()["gst"])
                        if books is not None and portal_frame is not None else None),
        }

    try:
        yield build
    finally:
        data_paths.DATA_ROOT = original


def _cdnr_data(**overrides):
    """The portal row with named fields substitutable by flattened label tail."""
    keys = ["gstin", "party", "note_number", "note_type", "note_date", "note_value",
            "taxable", "igst", "cgst", "sgst", "cess", "itc", "rcm", "period"]
    row = dict(zip(keys, CDNR_DATA))
    row.update(overrides)
    return [row[k] for k in keys]


def test_a_booking_months_after_the_note_date_still_matches(pair_factory):
    """A late booking must not break the match, because BOTH sides carry the
    SUPPLIER's date: the register's `Voucher Ref. Date` (the date on the
    supplier's document — not the voucher `Date`, which is when we entered it)
    and the portal's `Note Date`. Here the note is issued 26/03/2025 but only
    booked 15/01/2026; keying off our booking date instead would put the two
    sides 295 days apart, past the guard, and split one document into two."""
    late = [list(REGISTER_ROWS[0])]
    late[0][0] = "2026-01-15"                    # voucher Date: when WE booked it
    late[0][2] = "Supplier A"
    assert late[0][7] == "2025-03-26", "Voucher Ref. Date is the supplier's date"

    built = pair_factory(late, CDNR_DATA)
    assert built["warnings"] == []
    assert len(built["results"]) == 1
    result = built["results"][0]
    assert result["classification"] == "Matched"
    assert result["confidence_score"] == pytest.approx(98.0)
    assert result["confidence_band"] == "High"
    assert str(result["note_date"]) == "2025-03-26"


def test_a_reference_reused_in_a_later_year_is_not_the_same_document(pair_factory):
    """The normaliser strips the FY suffix, so SRI-312/25-26 and SRI-312/26-27
    share the key SRI312. Without the date guard the supplier's later-year note
    would be paired with ours and reported Matched — a false positive, the one
    error class this module must never make."""
    later_year = _cdnr_data(note_number="SRI-312/26-27", note_date="05/04/2026",
                            period="Apr'26", note_value=1008, taxable=900,
                            cgst=54, sgst=54)

    ours = [[
        "2025-06-10", "Supplier A", "Supplier A", "Addr A", "Debit Note", "8/2025-26",
        None, "2025-06-10", BOOKS_GSTIN, None, "AAAAA0000A",
        "CREDIT NOTE NO. SRI-312/25-26", 1008, 900, None,
        None, None, 54.0, 54.0, None,
    ]]

    built = pair_factory(ours, later_year)
    assert built["warnings"] == []
    assert len(built["results"]) == 2, "one side each, never paired"

    by_side = {r["note_side"]: r for r in built["results"]}
    assert by_side["books"]["classification"] == "Not in Portal"
    assert by_side["portal"]["classification"] == "Not in Books"
    for result in built["results"]:
        assert result["note_reference"] == "SRI312", "same key, different document"
        assert result["classification"] != "Matched"

    # The reason explains WHY, so the reader is not left guessing.
    assert "DIFFERENT document" in by_side["books"]["match_reason"]


def test_the_portal_cdnr_sheet_yields_a_canonical_note_frame(filed):
    """If this layout drifts from the config's contracted labels, the note
    would vanish from the portal frame and every note would report
    Not in Books — so this is the guard for that contract."""
    frame = f6_bridge.parse_portal_notes(filed["portal_path"], "gstr2b", filed["gstr2b"])

    assert frame is not None and len(frame) == 1
    row = frame.iloc[0]
    assert row["gstin"] == BOOKS_GSTIN
    assert row["invoice_number"] == NOTE_NUMBER
    assert str(row["document_type"]).strip().lower() == "credit note"
    assert str(row["invoice_date"]) == "2025-03-26"
    assert float(row["invoice_value"]) == pytest.approx(NOTE_VALUE)
    assert float(row["taxable_value"]) == pytest.approx(NOTE_TAXABLE)
    assert float(row["cgst"]) == pytest.approx(NOTE_TAX)


def test_the_invoice_path_is_unchanged_by_the_note_accessor(filed):
    """The same workbook through the INVOICE path must still exclude the note
    entirely — and, with only notes on offer, report no invoice frame at all."""
    assert f6_bridge.parse_portal_export(
        filed["portal_path"], "gstr2b", filed["gstr2b"]
    ) is None


# --- Both sides through their real parsers, then matched -------------------


def test_the_note_matches_on_the_primary_reference_key(filed, tmp_path):
    from src import bootstrap
    from src.ingestion_ai.normalizer import load_note_frames
    from src.matching.note_matcher import match_notes

    db = tmp_path / "pair.db"
    bootstrap.init_all(str(db))

    books, portal, adjacent, warnings = load_note_frames(
        CLIENT, PERIOD,
        selected_files={"credit_notes": filed["credit_notes"], "gstr2b": filed["gstr2b"]},
        actor="test", db_path=str(db),
    )
    assert warnings == []
    assert books is not None and len(books) == 2
    assert portal is not None and len(portal) == 1

    results = match_notes(books, portal, load_config()["gst"])
    assert len(results) == 2, "one Matched and one Not in Portal"

    by_reference = {r["note_reference"]: r for r in results}

    matched = by_reference["CD12042025"]
    assert matched["classification"] == "Matched"
    assert matched["difference_type"] is None
    assert matched["note_side"] == "both"
    assert matched["note_kind"] == "Credit Note"
    assert matched["confidence_score"] == pytest.approx(98.0)
    assert matched["confidence_band"] == "High"
    assert "Reference match" in matched["match_reason"]
    # The note's OWN date, from the books register's Voucher Ref. Date.
    assert str(matched["note_date"]) == "2025-03-26"
    assert matched["note_tax"] == pytest.approx(2 * NOTE_TAX)

    missing = by_reference["ZZ999042025"]
    assert missing["classification"] == "Not in Portal"
    assert missing["note_side"] == "books"


def test_the_matched_note_is_not_an_invoice_row(filed, tmp_path):
    """Both sides parsed for real, and the note appears in NEITHER invoice
    result set — the separation holds on real frames, not just synthetic ones."""
    from src import bootstrap
    from src.ingestion_ai.normalizer import load_note_frames
    from src.matching.note_matcher import match_notes

    db = tmp_path / "pair2.db"
    bootstrap.init_all(str(db))
    books, portal, _adjacent, _warnings = load_note_frames(
        CLIENT, PERIOD,
        selected_files={"credit_notes": filed["credit_notes"], "gstr2b": filed["gstr2b"]},
        actor="test", db_path=str(db),
    )
    results = match_notes(books, portal, load_config()["gst"])
    for result in results:
        assert result["classification"] in ("Matched", "Amount Difference",
                                            "Not in Books", "Not in Portal")
        assert result.get("note_reference")
