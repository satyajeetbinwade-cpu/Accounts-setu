"""Generate the SANITISED Purchase Register fixture (§3 rule 11).

The real client file (P.N. Sarees Pvt. Ltd., Aug 2026) must NOT be committed
until F6 §13 (specimen retention / DPDP) is decided. This script derives a
fixture with:

  * IDENTICAL numbers, layout and structure (so every golden total / golden
    row in the corrective build prompt §1.1 still holds byte-for-byte), and
  * a sanitised entity name, sanitised supplier names, and CHECKSUM-VALID
    FAKE GSTINs (state code preserved, because §1.1's inter-state vs local
    distinction depends on it: state 24 => IGST-only, state 07 => CGST+SGST).

Bill numbers and the "Purc. Type" column are preserved because the prompt's
golden-row table (§1.1) identifies rows by them, and because bill numbers are
not personal data.

Run:
    venv/bin/python tests/fixtures/make_purchase_register_fixture.py \
        "/path/to/PurchaseRegister(Bill-wise) (1).xlsx"

Idempotent: rewrites the fixture in place.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

FIXTURE_PATH = Path(__file__).resolve().parent / "purchase_register_sanitised_aug2026.xlsx"

# GSTIN checksum: mod-36 over the first 14 characters, alternating weights.
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_checksum(first14: str) -> str:
    """Compute the 15th (checksum) character of a GSTIN."""
    total = 0
    for i, ch in enumerate(first14):
        value = _GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += value // 36 + value % 36
    return _GSTIN_CHARS[(36 - (total % 36)) % 36]


def make_gstin(seed14: str) -> str:
    """Turn any 14-char GSTIN stem into a checksum-VALID GSTIN."""
    stem = seed14[:14].upper()
    return stem + gstin_checksum(stem)


# Sanitised supplier names, in the specimen's row order of first appearance.
# The GSTIN state code is preserved (07 = Delhi/local, 24 = Gujarat/inter-state).
_FAKE_SUPPLIERS: dict[str, tuple[str, str]] = {
    # original GSTIN -> (fake GSTIN stem (14 chars), fake name)
    "07AAXFS9006M1ZC": ("07AAQCS1234M1Z", "SURYA TRADERS & CO."),
    "07AACCR8869H1ZU": ("07AABCR5678H1Z", "ROOPAM TEXTILES (P) LTD."),
    "07AAMFJ9163F1Z1": ("07AAMFJ2468F1Z", "JAGDAMBA AND SONS INDIA LLP"),
    "07APLPT8045C1ZN": ("07APLPT1357C1Z", "MEENA FASHION"),
    "07AAGCP9332D1ZG": ("07AAGCP8642D1Z", "POOJA CREATIONS PVT. LTD."),
    "24DCNPB1786A1ZX": ("24DCNPB9753A1Z", "SWASTIK TRADING CO."),
    "07APRPG2889P1ZV": ("07APRPG6420P1Z", "PUSHPANJALI CREATIONS"),
}

_FAKE_ENTITY = "SAMPLE TEXTILES PVT. LTD."
_FAKE_ADDRESS = "12/34, SAMPLE MARKET, SAMPLE CITY-110001"


def build_fixture(source: Path) -> Path:
    raw = pd.read_excel(source, header=None, dtype=object)

    # Row 0: entity name. Row 1: address. Row 2: report title (kept — it is
    # the format's own title, not client data). Rows 3-4: period + filter.
    raw.iat[0, 0] = _FAKE_ENTITY
    raw.iat[1, 0] = _FAKE_ADDRESS

    gstin_map = {orig: make_gstin(stem) for orig, (stem, _n) in _FAKE_SUPPLIERS.items()}
    name_map = {orig: name for orig, (_stem, name) in _FAKE_SUPPLIERS.items()}

    for r in range(7, len(raw)):
        gstin = str(raw.iat[r, 4]).strip()
        if gstin in gstin_map:
            raw.iat[r, 4] = gstin_map[gstin]
            raw.iat[r, 3] = name_map[gstin]

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(FIXTURE_PATH, engine="openpyxl") as xw:
        raw.to_excel(xw, sheet_name="Sheet1", header=False, index=False)
    return FIXTURE_PATH


def _selftest() -> None:
    """Verify the generated fixture preserves every §1.1 golden figure."""
    df = pd.read_excel(FIXTURE_PATH, header=None, dtype=object)
    cols = [str(c).strip().lower() for c in df.iloc[6].tolist()]
    data = df.iloc[7:42]
    footer = df.iloc[43]

    def col(name: str) -> int:
        return cols.index(name)

    def num(series) -> float:
        return float(pd.to_numeric(series, errors="coerce").fillna(0).sum())

    taxable = num(data[col("igst txbl. amt @ 5%")]) + num(data[col("igst txbl. amt @ 18%")]) \
        + num(data[col("cgst txbl. amt @ 2.5%")]) + num(data[col("cgst txbl. amt @ 9%")])
    cgst = num(data[col("cgst tax amt @ 2.5%")]) + num(data[col("cgst tax amt @ 9%")])
    sgst = num(data[col("sgst tax amt @ 2.5%")]) + num(data[col("sgst tax amt @ 9%")])
    igst = num(data[col("igst tax amt @ 5%")]) + num(data[col("igst tax amt @ 18%")])
    rounding = num(data[col("other amt.")])
    invoice = num(data[col("bill amount")])

    checks = {
        "rows": (len(data), 35),
        "taxable_value": (round(taxable, 2), 722707.00),
        "cgst": (round(cgst, 2), 47154.43),
        "sgst": (round(sgst, 2), 47154.43),
        "igst": (round(igst, 2), 4204.70),
        "rounding_adjustment": (round(rounding, 2), 0.44),
        "invoice_value": (round(invoice, 2), 821221.00),
        "footer_total": (round(float(footer[col("bill amount")]), 2), 821221.00),
    }
    ok = True
    for label, (got, want) in checks.items():
        status = "OK " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  [{status}] {label}: {got} (expected {want})")

    # Every GSTIN must be checksum-valid.
    for r in range(7, 42):
        g = str(df.iat[r, 4]).strip()
        assert len(g) == 15 and gstin_checksum(g[:14]) == g[14], f"invalid fixture GSTIN {g}"
    print("  [OK ] all 35 GSTINs checksum-valid")
    if not ok:
        raise SystemExit("Fixture self-test FAILED")
    print("Fixture self-test passed.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    out = build_fixture(Path(sys.argv[1]))
    print(f"Wrote {out}")
    _selftest()
