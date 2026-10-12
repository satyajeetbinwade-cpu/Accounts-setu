"""GST state codes — the C1 table that says what a 2-digit code IS.

WHY A DEDICATED TABLE
---------------------
``regulatory_rules`` holds dated free text ('18.0%', 'ITC blocked'), which is
right for a rate but wrong for a state code: the Phase-3 checks need three
STRUCTURED facts per code —

* the state/UT name, to render and to match a printed place of supply,
* the short aliases an invoice actually prints ("UP", "U.P.", "Uttar Pradesh"),
* whether the UT has NO legislature, because that decides UTGST vs SGST.

Parsing those out of a notes column would be a guess dressed up as data. So the
codes live in their own table with real columns, seeded with ``SEED_AS_OF`` and
flagged UNVERIFIED in ``notes`` exactly like the other C1 seeds.

EVERYTHING HERE IS SEED DATA, NOT A REGULATORY FACT
---------------------------------------------------
The list below is written from general knowledge of the GST state-code series.
It has NOT been verified against the GST portal, so every row carries
``last_reviewed_at = NULL`` and a note saying so; the C1 screen's existing
"seed — unverified" convention renders it as unreviewed. A Manager reviewing a
row (``mark_state_code_reviewed``) is the only thing that clears it.

Two codes beyond the 38 states/UTs are included on purpose:

* ``96`` Other Country and ``97`` Other Territory — an invoice that carries one
  is an export/import/special-territory case, and the Phase-3 expected-tax-type
  rule must say "cannot determine" rather than invent an expectation.
* ``99`` Centre Jurisdiction — historically used for oil/gas and some
  centre-administered registrations.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

# The stated "as of" date for this seed. Matches rules.seed.SEED_AS_OF's
# convention (kept local so this module has no import cycle with seed.py).
SEED_AS_OF = "2026-09-14"

STATE_CODE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS gst_state_codes (
        code             TEXT PRIMARY KEY,      -- the 2-digit code as printed, e.g. '07'
        name             TEXT    NOT NULL,      -- 'Delhi'
        -- Short forms an invoice may print, ';'-separated: 'DL;Delhi (NCT);NCT of Delhi'
        aliases          TEXT    NOT NULL DEFAULT '',
        -- 1 = a Union Territory WITHOUT a legislature, so UTGST applies instead
        -- of SGST (GST Act s.7(1) proviso). Delhi, Puducherry and Jammu &
        -- Kashmir HAVE legislatures and therefore levy SGST.
        utgst            INTEGER NOT NULL DEFAULT 0,
        -- 1 = not a state/UT: 96 Other Country, 97 Other Territory,
        -- 99 Centre Jurisdiction. The expected-tax-type rule treats these as
        -- "cannot determine" rather than comparing their tax heads.
        special          INTEGER NOT NULL DEFAULT 0,
        seed_as_of       TEXT    NOT NULL,
        last_reviewed_at TEXT,                  -- NULL => seed-unverified
        notes            TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_gst_state_codes_name ON gst_state_codes(name)
    """,
)

_UNVERIFIED = (
    "UNVERIFIED — seeded from general knowledge of the GST state-code series; "
    "confirm against the GST portal before relying on it for a filing."
)

# (code, name, aliases, utgst, special, note)
#
# `utgst=1` is set ONLY for the Union Territories without a legislature, which
# is the set the Phase-3 rule needs: Ladakh, Chandigarh, Dadra and Nagar Haveli
# and Daman and Diu, Lakshadweep, Andaman and Nicobar Islands — plus the
# historical Daman and Diu (25), which is retained because old invoices carry
# it and it was itself a UT without a legislature.
SEED_STATE_CODES: tuple[tuple[str, str, str, int, int, Optional[str]], ...] = (
    ("01", "Jammu and Kashmir", "JK;J&K;Jammu & Kashmir;Jammu and Kashmir (UT)", 0, 0, None),
    ("02", "Himachal Pradesh", "HP;Himachal", 0, 0, None),
    ("03", "Punjab", "PB;Punjab", 0, 0, None),
    ("04", "Chandigarh", "CH;Chandigarh (UT)", 1, 0, None),
    ("05", "Uttarakhand", "UK;UA;Uttaranchal", 0, 0, "Renamed from Uttaranchal; old invoices may print 'UA'."),
    ("06", "Haryana", "HR;Haryana", 0, 0, None),
    ("07", "Delhi", "DL;Delhi (NCT);NCT of Delhi;National Capital Territory of Delhi;New Delhi", 0, 0,
     "A UT WITH a legislature, so SGST applies (NOT UTGST) — Delhi is the case most often mistaken for UTGST."),
    ("08", "Rajasthan", "RJ;Rajasthan", 0, 0, None),
    ("09", "Uttar Pradesh", "UP;U.P.;Uttar Pradesh", 0, 0, None),
    ("10", "Bihar", "BR;Bihar", 0, 0, None),
    ("11", "Sikkim", "SK;Sikkim", 0, 0, None),
    ("12", "Arunachal Pradesh", "AR;Arunachal", 0, 0, None),
    ("13", "Nagaland", "NL;Nagaland", 0, 0, None),
    ("14", "Manipur", "MN;Manipur", 0, 0, None),
    ("15", "Mizoram", "MZ;Mizoram", 0, 0, None),
    ("16", "Tripura", "TR;Tripura", 0, 0, None),
    ("17", "Meghalaya", "ML;Meghalaya", 0, 0, None),
    ("18", "Assam", "AS;Assam", 0, 0, None),
    ("19", "West Bengal", "WB;West Bengal", 0, 0, None),
    ("20", "Jharkhand", "JH;Jharkhand", 0, 0, None),
    ("21", "Odisha", "OD;OR;Orissa", 0, 0, "Renamed from Orissa; old invoices may print 'OR'."),
    ("22", "Chhattisgarh", "CG;CT;Chhatisgarh", 0, 0, None),
    ("23", "Madhya Pradesh", "MP;Madhya Pradesh", 0, 0, None),
    ("24", "Gujarat", "GJ;Gujarat", 0, 0, None),
    ("25", "Daman and Diu", "DD;Daman & Diu", 1, 0,
     "Historical UT (merged into 26 on 2020-01-26). Old invoices still carry it; it had no legislature, so UTGST."),
    ("26", "Dadra and Nagar Haveli and Daman and Diu", "DN;DH;DNH;DNH and DD;Dadra and Nagar Haveli", 1, 0,
     "Merged UT since 2020-01-26; no legislature, so UTGST."),
    ("27", "Maharashtra", "MH;Maharashtra", 0, 0, None),
    ("28", "Andhra Pradesh (old, undivided)", "AP (old);Andhra Pradesh (pre-2014)", 0, 0,
     "Pre-2014 undivided Andhra Pradesh. Since Telangana (36) split, new registrations use 37; retained so an old "
     "GSTIN is not reported as an unknown code."),
    ("29", "Karnataka", "KA;Karnataka", 0, 0, None),
    ("30", "Goa", "GA;Goa", 0, 0, None),
    ("31", "Lakshadweep", "LD;Lakshadweep", 1, 0, "No legislature, so UTGST."),
    ("32", "Kerala", "KL;Kerala", 0, 0, None),
    ("33", "Tamil Nadu", "TN;Tamil Nadu", 0, 0, None),
    ("34", "Puducherry", "PY;Pondicherry;Puducherry (UT)", 0, 0,
     "A UT WITH a legislature, so SGST applies (NOT UTGST). Renamed from Pondicherry."),
    ("35", "Andaman and Nicobar Islands", "AN;Andaman & Nicobar;Andaman and Nicobar", 1, 0,
     "No legislature, so UTGST."),
    ("36", "Telangana", "TG;TS;Telangana", 0, 0, None),
    ("37", "Andhra Pradesh", "AP;Andhra Pradesh", 0, 0, None),
    ("38", "Ladakh", "LA;Ladakh", 1, 0, "No legislature, so UTGST."),
    ("96", "Other Country", "Other Country;Export;Import", 0, 1,
     "Not a state: an export/import or a foreign supplier. The expected-tax-type rule cannot determine an "
     "expectation from it."),
    ("97", "Other Territory", "Other Territory", 0, 1,
     "Not a state: a special territory. Cannot determine an expectation from it."),
    ("99", "Centre Jurisdiction", "Centre Jurisdiction;Centre", 0, 1,
     "Not a state: some centre-administered registrations (historically oil/gas). Cannot determine an expectation."),
)


def execute_state_code_schema(conn: sqlite3.Connection) -> None:
    """Create the table. Used by the migration (no commit) and by init (commit)."""
    for statement in STATE_CODE_STATEMENTS:
        conn.execute(statement)


def upsert_state_code(
    conn: sqlite3.Connection, *, code: str, name: str, aliases: str,
    utgst: int, special: int, notes: Optional[str],
) -> None:
    """Insert-or-update one code. Never clears ``last_reviewed_at``."""
    existing = conn.execute(
        "SELECT code FROM gst_state_codes WHERE code = ?", (code,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO gst_state_codes (code, name, aliases, utgst, special, seed_as_of, "
            "last_reviewed_at, notes) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
            (code, name, aliases, int(utgst), int(special), SEED_AS_OF, notes),
        )
        return
    conn.execute(
        "UPDATE gst_state_codes SET name = ?, aliases = ?, utgst = ?, special = ? WHERE code = ?",
        (name, aliases, int(utgst), int(special), code),
    )


def seed_state_codes(conn: sqlite3.Connection) -> int:
    """Seed every code. Idempotent, additive — a Manager's edit survives."""
    count = 0
    for code, name, aliases, utgst, special, note in SEED_STATE_CODES:
        notes = _UNVERIFIED if not note else f"{note} {_UNVERIFIED}"
        upsert_state_code(
            conn, code=code, name=name, aliases=aliases,
            utgst=utgst, special=special, notes=notes,
        )
        count += 1
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Accessors (the ONLY place the table's shape is read)
# ---------------------------------------------------------------------------


def list_state_codes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "code": row[0], "name": row[1], "aliases": row[2],
            "utgst": bool(row[3]), "special": bool(row[4]),
            "seed_as_of": row[5], "last_reviewed_at": row[6], "notes": row[7],
        }
        for row in conn.execute(
            "SELECT code, name, aliases, utgst, special, seed_as_of, last_reviewed_at, notes "
            "FROM gst_state_codes ORDER BY code"
        )
    ]


def get_state_code(conn: sqlite3.Connection, code: Optional[str]) -> Optional[dict[str, Any]]:
    if not code:
        return None
    row = conn.execute(
        "SELECT code, name, aliases, utgst, special, seed_as_of, last_reviewed_at, notes "
        "FROM gst_state_codes WHERE code = ?", (str(code).strip(),),
    ).fetchone()
    if row is None:
        return None
    return {
        "code": row[0], "name": row[1], "aliases": row[2], "utgst": bool(row[3]),
        "special": bool(row[4]), "seed_as_of": row[5], "last_reviewed_at": row[6], "notes": row[7],
    }


def mark_state_code_reviewed(conn: sqlite3.Connection, code: str, *, at: str) -> None:
    conn.execute("UPDATE gst_state_codes SET last_reviewed_at = ? WHERE code = ?", (at, code))


def add_state_code_alias(conn: sqlite3.Connection, code: str, alias: str) -> None:
    """Add one alias to a code (used when a client's paperwork prints a form we
    had not listed). Never removes an existing alias."""
    existing = conn.execute(
        "SELECT aliases FROM gst_state_codes WHERE code = ?", (code,)
    ).fetchone()
    if existing is None:
        return
    aliases = [a.strip() for a in str(existing[0] or "").split(";") if a.strip()]
    if alias.strip() and alias.strip() not in aliases:
        aliases.append(alias.strip())
        conn.execute(
            "UPDATE gst_state_codes SET aliases = ? WHERE code = ?", (";".join(aliases), code),
        )


# ---------------------------------------------------------------------------
# Matching a printed place of supply against the table
# ---------------------------------------------------------------------------


def normalise_place(text: Optional[str]) -> str:
    """Fold a printed place-of-supply / state name for comparison.

    Casefolds and turns every non-alphanumeric run into a single space, so
    ``"U.P."`` → ``"u p"`` and ``"Delhi (NCT)"`` → ``"delhi nct"``.
    """
    if text is None:
        return ""
    folded = "".join(ch if ch.isalnum() else " " for ch in str(text).casefold())
    return " ".join(folded.split())


def _alias_variants(code: str, name: str, aliases: str) -> set[str]:
    out = {normalise_place(code), normalise_place(name)}
    # "U.P." folds to "u p"; also index the squeezed form so either spelling of
    # an abbreviation resolves.
    out.add(normalise_place(code).replace(" ", ""))
    out.add(normalise_place(name).replace(" ", ""))
    for raw in str(aliases or "").split(";"):
        value = normalise_place(raw)
        if value:
            out.add(value)
            out.add(value.replace(" ", ""))
    return {v for v in out if v}


def resolve_state_code(
    conn: sqlite3.Connection, text: Optional[str],
) -> Optional[dict[str, Any]]:
    """The state row a printed place of supply refers to, or None.

    Two passes, in this order:

    1. EXACT on the folded text against the code, the name, or any alias.
    2. Whole-word containment, for a printed value carrying extra words around
       the state name (``"Uttar Pradesh (East)"``). Aliases shorter than 4
       characters are excluded from this pass — a 2-letter abbreviation hiding
       inside a longer word would resolve the WRONG state.

    An exact tie is broken towards the HIGHEST code, i.e. the current
    registration series: ``"AP"`` resolves to 37 (Andhra Pradesh after the 2014
    split) rather than 28 (the pre-split undivided code).
    """
    needle = normalise_place(text)
    if not needle:
        return None
    rows = list_state_codes(conn)
    squeezed = needle.replace(" ", "")

    exact: list[dict[str, Any]] = []
    for row in rows:
        variants = _alias_variants(row["code"], row["name"], row["aliases"])
        if needle in variants or squeezed in variants:
            exact.append(row)
    if exact:
        exact.sort(key=lambda r: (not r["special"], -int(r["code"])))
        return exact[0]

    words = set(needle.split())
    partial: list[dict[str, Any]] = []
    for row in rows:
        for variant in _alias_variants(row["code"], row["name"], row["aliases"]):
            if len(variant.replace(" ", "")) < 4:
                continue
            if variant in words or set(variant.split()) <= words:
                partial.append(row)
                break
    if partial:
        partial.sort(key=lambda r: (not r["special"], -int(r["code"])))
        return partial[0]
    return None


def state_code_of_gstin(gstin: Optional[str]) -> Optional[str]:
    """The 2-digit state code a GSTIN starts with, or None when it cannot be one."""
    text = "".join(str(gstin or "").split()).upper()
    if len(text) < 2 or not text[:2].isdigit():
        return None
    return text[:2]


def is_utgst_code(conn: sqlite3.Connection, code: Optional[str]) -> bool:
    """Does this code levy UTGST instead of SGST (a UT without a legislature)?"""
    row = get_state_code(conn, code)
    return bool(row and row["utgst"])


def is_special_code(conn: sqlite3.Connection, code: Optional[str]) -> bool:
    """Is this code a non-state jurisdiction (Other Country / Other Territory /
    Centre Jurisdiction)? The expected-tax-type rule cannot determine an
    expectation from one of these."""
    row = get_state_code(conn, code)
    return bool(row and row["special"])
