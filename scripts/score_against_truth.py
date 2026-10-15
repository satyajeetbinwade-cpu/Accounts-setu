#!/usr/bin/env python3
"""Score Setu's triage + extraction output against a hand-read answer key.

Read-only, standard library only:

    python scripts/score_against_truth.py --db PATH --upload ID --truth PATH [--out report.txt]

The database is opened with sqlite3 URI ``mode=ro`` and is never written. Table
and column names are DISCOVERED from the schema (never guessed): the script looks
for the table that carries the columns it needs, so a rename shows up as an
error instead of a silent zero.

Triage is scored by page overlap (``meta.how_to_score``); extraction is scored
per key field with normalisation (amounts to the paisa, dates to ISO, strings
case/space/punctuation-insensitive, GSTINs exact). A handwritten figure that
appears as a field VALUE is a hard failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Schema discovery
# ---------------------------------------------------------------------------

#: columns each logical table must carry, and the names we would rather use
_TABLE_HINTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "documents": (("category", "label", "excluded", "upload_id"), ("invoice_documents", "documents")),
    "regions": (("document_id", "page", "is_active"), ("document_regions",)),
    "extractions": (("document_id", "status", "skipped_reason"), ("invoice_doc_extractions",)),
    "fields": (("document_id", "field_key", "value_a", "reads_agree"), ("invoice_doc_fields",)),
    "annotations": (("document_id", "text_verbatim"), ("invoice_doc_annotations",)),
    "checks": (("document_id", "check_key", "status", "detail"), ("invoice_doc_checks",)),
    "pages": (("page", "duplicate_of_page", "status"), ("invoice_triage_pages",)),
    "region_log": (("document_id", "action", "after_json"), ("document_region_log",)),
    "derived": (("document_id", "key", "value"), ("invoice_doc_derived",)),
}


def _table_names(con: sqlite3.Connection) -> list[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return [r[0] for r in rows]


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute('PRAGMA table_info("%s")' % table)]


def discover_schema(con: sqlite3.Connection) -> dict[str, str]:
    """Map each logical table to a real table name found in the schema."""
    schema: dict[str, str] = {}
    tables = _table_names(con)
    for logical, (required, preferred) in _TABLE_HINTS.items():
        needed = set(required)
        found: Optional[str] = None
        for name in preferred:
            if name in tables and needed <= set(_columns(con, name)):
                found = name
                break
        if found is None:
            for name in tables:
                if needed <= set(_columns(con, name)):
                    found = name
                    break
        if found is None:
            raise SystemExit(
                f"schema: no table carries {sorted(required)} (needed for {logical}); "
                f"looked in {len(tables)} tables"
            )
        schema[logical] = found
    return schema


def col(schema: dict[str, Any], table: str, *names: str) -> str:
    """First present column of ``table`` among ``names`` (fails loudly)."""
    have = schema.get("columns", {}).get(table, ())
    for n in names:
        if n in have:
            return n
    raise SystemExit(f"schema: none of {names} in {table}")


def open_read_only(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise SystemExit(f"database not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_CURRENCY = "₹$€£"
_DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y", "%Y/%m/%d",
    "%d-%b-%y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%d.%m.%Y",
    "%d.%m.%y", "%Y%m%d",
)


def norm_number(value: Any) -> Optional[float]:
    """Money/number to the paisa: ``'₹ 1,180.00'`` -> ``1180.0``."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 2)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("\u00a0", " ")
    for ch in _CURRENCY:
        text = text.replace(ch, "")
    text = text.replace(",", "").replace(" ", "")
    negative = text.startswith("(-)") or (text.startswith("(") and text.endswith(")")) or text.startswith("-")
    text = text.strip("()").replace("(-)", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    if negative and number > 0:
        number = -number
    return round(number, 2)


def norm_date(value: Any) -> Optional[str]:
    """Any common date form to ISO ``YYYY-MM-DD``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T].*)?", text):
        return text[:10]
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    cleaned = cleaned.replace(",", " ").replace(".", "-").replace("/", "-")
    cleaned = re.sub(r"\s+", " ", cleaned).strip("- ")
    for fmt in _DATE_FORMATS:
        try:
            import datetime as _dt
            return _dt.datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    match = re.search(r"\d{1,2}[- ][A-Za-z]{3,9}[- ]\d{2,4}", text)
    if match:
        for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%B-%Y"):
            try:
                import datetime as _dt
                return _dt.datetime.strptime(match.group(0).replace(" ", "-"), fmt).date().isoformat()
            except ValueError:
                continue
    return None


def norm_text(value: Any) -> str:
    """Case/space/punctuation-insensitive comparison form."""
    if value is None:
        return ""
    text = str(value).casefold().replace("\u00a0", " ")
    for ch in _CURRENCY:
        text = text.replace(ch, " ")
    text = re.sub(r"[^0-9a-z]+", "", text)
    return text


def norm_gstin(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value)).upper()


_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")


def field_kind(field_key: str) -> str:
    key = (field_key or "").casefold()
    if key.endswith("gstin"):
        return "gstin"
    if key.endswith("_date") and key != "reference_no_date":
        return "date"
    if key == "reference_no_date":
        return "text_date"
    if key == "total_in_words":
        return "text"
    if re.search(r"(amount|total|value|tax|rate|quantity|round_off|charges|fee|balance)$", key) or \
            re.search(r"\.(amount|rate|quantity)$", key):
        return "number"
    return "text"


def normalised(field_key: str, value: Any) -> Any:
    kind = field_kind(field_key)
    if kind == "number":
        return norm_number(value)
    if kind == "date":
        return norm_date(value)
    if kind == "gstin":
        return norm_gstin(value)
    if kind == "text_date":
        iso = norm_date(value)
        if iso:
            return iso
        return norm_text(value)
    return norm_text(value)


def values_equal(field_key: str, expected: Any, actual: Any) -> bool:
    kind = field_kind(field_key)
    if kind == "number":
        e, a = norm_number(expected), norm_number(actual)
        if e is None or a is None:
            return False
        return abs(e - a) <= 0.005
    if kind == "gstin":
        e, a = norm_gstin(expected), norm_gstin(actual)
        return bool(e) and e == a
    if kind in ("date", "text_date"):
        e, a = normalised(field_key, expected), normalised(field_key, actual)
        return bool(e) and e == a
    e, a = norm_text(expected), norm_text(actual)
    if not e:
        return not a
    return e == a


# ---------------------------------------------------------------------------
# Loading Setu's side
# ---------------------------------------------------------------------------


class SetuDoc:
    __slots__ = ("document_id", "ordinal", "category", "label", "excluded",
                 "excluded_reason", "flags", "pages", "extraction_status",
                 "skipped_reason", "fields", "annotations", "checks")

    def __init__(self, **kw: Any) -> None:
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))
        if self.pages is None:
            self.pages = set()
        if self.fields is None:
            self.fields = {}
        if self.annotations is None:
            self.annotations = []
        if self.checks is None:
            self.checks = []

    def value(self, field_key: str) -> Optional[str]:
        entry = self.fields.get(field_key)
        if not entry:
            return None
        return entry.get("value")

    def empty(self, field_key: str) -> bool:
        entry = self.fields.get(field_key)
        if not entry:
            return True
        return not str(entry.get("value") or "").strip()

    def numbers(self) -> set[float]:
        out: set[float] = set()
        for entry in self.fields.values():
            number = norm_number(entry.get("value"))
            if number is not None:
                out.add(number)
        return out


def load_setu(con: sqlite3.Connection, schema: dict[str, Any], upload_id: int) -> dict[str, Any]:
    docs_table = schema["documents"]
    columns = schema["columns"][docs_table]
    config = {
        "document_id": col(schema, docs_table, "document_id", "id"),
        "ordinal": col(schema, docs_table, "ordinal"),
        "category": col(schema, docs_table, "category"),
        "label": col(schema, docs_table, "label"),
        "excluded": col(schema, docs_table, "excluded"),
        "excluded_reason": col(schema, docs_table, "excluded_reason"),
        "flags": col(schema, docs_table, "flags_json"),
        "is_active": col(schema, docs_table, "is_active"),
        "upload_id": col(schema, docs_table, "upload_id"),
    }

    rows = con.execute(
        'SELECT {document_id} AS did, {ordinal} AS ordinal, {category} AS category, '
        '{label} AS label, {excluded} AS excluded, {excluded_reason} AS reason, '
        '{flags} AS flags FROM "{table}" WHERE {upload_id} = ? AND {is_active} = 1 '
        "ORDER BY {ordinal}".format(table=docs_table, **config),
        (upload_id,),
    ).fetchall()
    if not rows:
        raise SystemExit(f"no active documents for upload {upload_id}")

    docs: dict[int, SetuDoc] = {}
    for r in rows:
        try:
            flags = json.loads(r["flags"] or "[]")
        except (TypeError, ValueError):
            flags = []
        docs[int(r["did"])] = SetuDoc(
            document_id=int(r["did"]), ordinal=int(r["ordinal"]), category=r["category"],
            label=r["label"] or "", excluded=bool(r["excluded"]),
            excluded_reason=r["reason"], flags=flags,
        )

    # regions -> pages
    reg_table = schema["regions"]
    reg = {k: col(schema, reg_table, k) for k in ("document_id", "page", "is_active")}
    for r in con.execute(
        'SELECT {document_id} AS did, {page} AS page FROM "{t}" WHERE {is_active} = 1'.format(
            t=reg_table, **reg)):
        doc = docs.get(int(r["did"]))
        if doc is not None:
            doc.pages.add(int(r["page"]))

    # extractions -> status (the ACTIVE pass only)
    ex_table = schema["extractions"]
    ex_cols = schema["columns"][ex_table]
    ex = {
        "document_id": col(schema, ex_table, "document_id"),
        "status": col(schema, ex_table, "status"),
        "skipped_reason": col(schema, ex_table, "skipped_reason"),
        "extraction_id": col(schema, ex_table, "extraction_id", "id"),
    }
    active_filter = ""
    if "is_active" in ex_cols:
        active_filter = ' AND "is_active" = 1'
    extraction_ids: dict[int, int] = {}
    for r in con.execute(
        'SELECT {document_id} AS did, {status} AS status, {skipped_reason} AS reason, '
        '{extraction_id} AS eid FROM "{t}" WHERE 1 = 1{active}'.format(
            t=ex_table, active=active_filter, **ex)):
        doc = docs.get(int(r["did"]))
        if doc is None:
            continue
        doc.extraction_status = r["status"]
        doc.skipped_reason = r["reason"]
        extraction_ids[int(r["did"])] = int(r["eid"])

    # fields
    f_table = schema["fields"]
    f_cols = schema["columns"][f_table]
    f = {
        "document_id": col(schema, f_table, "document_id"),
        "extraction_id": col(schema, f_table, "extraction_id"),
        "field_key": col(schema, f_table, "field_key"),
        "status": col(schema, f_table, "status"),
        "value_a": col(schema, f_table, "value_a"),
        "value_b": col(schema, f_table, "value_b"),
        "reads_agree": col(schema, f_table, "reads_agree"),
        "is_handwritten": col(schema, f_table, "is_handwritten"),
    }
    for r in con.execute(
        'SELECT {document_id} AS did, {extraction_id} AS eid, {field_key} AS key, '
        '{status} AS status, {value_a} AS a, {value_b} AS b, {reads_agree} AS agree, '
        '{is_handwritten} AS hand FROM "{t}"'.format(t=f_table, **f)):
        doc = docs.get(int(r["did"]))
        if doc is None or not r["key"]:
            continue
        if doc.extraction_status is not None and int(r["eid"]) != extraction_ids.get(int(r["did"])):
            continue  # a superseded pass, not this document's current read
        value = r["a"] if str(r["a"] or "").strip() else r["b"]
        doc.fields[str(r["key"])] = {
            "value": value, "value_a": r["a"], "value_b": r["b"],
            "status": r["status"], "reads_agree": r["agree"],
            "is_handwritten": bool(r["hand"]),
        }

    # annotations + checks
    a_table = schema["annotations"]
    a = {k: col(schema, a_table, k) for k in ("document_id", "text_verbatim", "used_as_value")} \
        if "used_as_value" in schema["columns"][a_table] else \
        {"document_id": col(schema, a_table, "document_id"),
         "text_verbatim": col(schema, a_table, "text_verbatim"), "used_as_value": None}
    for r in con.execute('SELECT {document_id} AS did, {text_verbatim} AS text{used} FROM "{t}"'.format(
            t=a_table, used=(", %s AS used" % a["used_as_value"]) if a["used_as_value"] else "", **a)):
        doc = docs.get(int(r["did"]))
        if doc is not None:
            doc.annotations.append({"text": r["text"], "used_as_value": bool(r["used"]) if a["used_as_value"] else None})

    c_table = schema["checks"]
    c = {k: col(schema, c_table, k) for k in ("document_id", "check_key", "status", "detail")}
    for r in con.execute('SELECT {document_id} AS did, {check_key} AS key, {status} AS status, '
                         '{detail} AS detail FROM "{t}"'.format(t=c_table, **c)):
        doc = docs.get(int(r["did"]))
        if doc is not None:
            doc.checks.append({"key": r["key"], "status": r["status"], "detail": r["detail"]})

    # page rows (duplicate detection) + region log
    p_table = schema["pages"]
    p = {k: col(schema, p_table, k) for k in ("page", "duplicate_of_page", "status")}
    run_filter = ""
    p_cols = schema["columns"][p_table]
    if "run_id" in p_cols and "run_id" in columns:
        run = con.execute(
            'SELECT MAX({run_id}) AS r FROM "{t}" WHERE {upload_id} = ?'.format(
                t=docs_table, run_id=col(schema, docs_table, "run_id"), upload_id=config["upload_id"]),
            (upload_id,)).fetchone()
        if run and run["r"] is not None:
            run_filter = ' WHERE "run_id" = %d' % int(run["r"])
    pages: dict[int, dict[str, Any]] = {}
    for r in con.execute('SELECT {page} AS page, {duplicate_of_page} AS dup, {status} AS status '
                         'FROM "{t}"{where}'.format(t=p_table, where=run_filter, **p)):
        pages[int(r["page"])] = {"duplicate_of_page": r["dup"], "status": r["status"]}

    lg_table = schema["region_log"]
    lg = {k: col(schema, lg_table, k) for k in ("document_id", "action", "after_json")}
    log = []
    for r in con.execute('SELECT {document_id} AS did, {action} AS action, {after_json} AS after '
                         'FROM "{t}"'.format(t=lg_table, **lg)):
        doc = docs.get(int(r["did"])) if r["did"] is not None else None
        log.append({"document_id": r["did"], "action": r["action"], "after_json": r["after"],
                    "in_upload": doc is not None})

    return {"docs": docs, "pages": pages, "log": log, "upload_id": upload_id}


def load_truth(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        raise SystemExit(f"answer key not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        truth = json.load(handle)
    truth.setdefault("documents", [])
    for doc in truth["documents"]:
        doc.setdefault("pages", [])
    return truth


# ---------------------------------------------------------------------------
# Triage scoring
# ---------------------------------------------------------------------------

_STOPWORDS = {"the", "of", "a", "and", "for", "to", "slip", "invoice", "copy", "page",
              "top", "bottom", "left", "right", "strip", "region"}


def _tokens(text: str) -> set[str]:
    raw = re.split(r"[^0-9a-z]+", str(text or "").casefold())
    return {t for t in raw if t and t not in _STOPWORDS}


def _label_similarity(truth_doc: dict[str, Any], setu_label: str) -> float:
    wanted = _tokens(truth_doc.get("doc_type", "")) | _tokens(truth_doc.get("issuer", "")) | \
        _tokens(truth_doc.get("region", ""))
    have = _tokens(setu_label)
    if not wanted or not have:
        return 0.0
    return len(wanted & have) / float(len(wanted))


def _off_by_one(a: str, b: str) -> bool:
    """One substitution, insertion, deletion or adjacent transposition away."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        if sum(1 for x, y in zip(a, b) if x != y) == 1:
            return True
        # a reading that swapped two adjacent letters, e.g. DTDC / DDTC
        return any(a[:i] + a[i + 1] + a[i] + a[i + 2:] == b for i in range(len(a) - 1))
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return any(long[:i] + long[i + 1:] == short for i in range(len(long)))


def _label_has_name(name: str, labels: Iterable[str], *, fuzzy: bool = False) -> bool:
    """Is ``name`` on one of the labels? ``fuzzy`` tolerates a one-letter typo."""
    wanted = name.casefold()
    if not wanted:
        return False
    for label in labels:
        text = (label or "").casefold()
        if wanted in text:
            return True
        if fuzzy and len(wanted) >= 4:
            if any(len(t) >= 4 and _off_by_one(t, wanted) for t in re.split(r"[^a-z]+", text)):
                return True
    return False


def match_documents(truth_docs: list[dict[str, Any]], setu_docs: dict[int, SetuDoc]) -> dict[str, int]:
    """One-to-one match by page overlap, broken by label similarity then size."""
    candidates: list[tuple[float, float, int, int, str, int]] = []
    for t in truth_docs:
        pages = set(int(p) for p in t.get("pages") or [])
        if not pages:
            continue
        for doc in setu_docs.values():
            overlap = len(pages & doc.pages)
            if overlap == 0:
                continue
            sim = _label_similarity(t, doc.label or "")
            # overlap first, then label, then the tighter page span, then ordinal
            candidates.append((overlap, sim, -len(doc.pages), -doc.ordinal, t["id"], doc.document_id))
    candidates.sort(reverse=True)
    matched: dict[str, int] = {}
    used_docs: set[int] = set()
    for overlap, _sim, _span, _ord, tid, did in candidates:
        if tid in matched or did in used_docs:
            continue
        matched[tid] = did
        used_docs.add(did)
    return matched


def score_triage(truth: dict[str, Any], setu: dict[str, Any]) -> dict[str, Any]:
    docs: dict[int, SetuDoc] = setu["docs"]
    truth_docs = truth["documents"]
    matched = match_documents(truth_docs, docs)

    rows: list[dict[str, Any]] = []
    for t in truth_docs:
        pages = set(int(p) for p in t.get("pages") or [])
        did = matched.get(t["id"])
        entry: dict[str, Any] = {
            "id": t["id"], "pages": sorted(pages), "expected": t.get("expected_category"),
            "acceptable": t.get("acceptable_categories") or [],
            "extract": t.get("extract"), "matched": did, "doc": None,
            "pages_ok": False, "category_ok": None, "notes": [],
        }
        if did is not None:
            doc = docs[did]
            entry["doc"] = doc
            entry["setu_pages"] = sorted(doc.pages)
            entry["setu_category"] = doc.category
            entry["excluded"] = doc.excluded
            entry["pages_ok"] = set(doc.pages) == pages
            entry["pages_ok_acceptable"] = entry["pages_ok"]
            acceptable = set(entry["acceptable"]) | {entry["expected"]}
            if entry["extract"] == "ca_to_decide":
                entry["category_ok"] = None  # the CA decides; report the choice only
                entry["notes"].append("CA to decide")
            else:
                entry["category_ok"] = doc.category in acceptable
            if pages and (pages - doc.pages):
                gone = sorted(pages - doc.pages)
                entry["notes"].append("missing pages %s" % gone)
                # A page the key lists as not-a-separate-document may sit in an
                # EXCLUDED Setu document (the banner pages of T14/T15): that is the
                # key's own acceptable_alternative, not a page-range failure. A
                # missing page that became its own ORDINARY document is a real
                # split failure, so only exclusions are accepted here.
                holders = [other for other in docs.values()
                           if other.document_id != doc.document_id and (other.pages & set(gone))]
                if holders and all(h.excluded for h in holders) and doc.extraction_status != "complete":
                    entry["pages_ok_acceptable"] = True
                    entry["notes"].append("page(s) %s sit in excluded document(s) %s — key allows" % (
                        gone, sorted(h.document_id for h in holders)))
            if doc.pages - pages:
                entry["notes"].append("extra pages %s" % sorted(doc.pages - pages))
            if doc.excluded:
                entry["notes"].append("excluded by Setu (%s)" % (doc.excluded_reason or "no reason"))
            if entry["extract"] is False and doc.extraction_status == "complete":
                entry["notes"].append("MISS: extract=false but Setu extracted it")
        else:
            entry["notes"].append("no Setu document")
        rows.append(entry)

    matched_ids = set(matched.values())
    extras = [d for d in docs.values() if d.document_id not in matched_ids and not d.excluded]
    excluded_docs = [d for d in docs.values() if d.excluded]

    truth_by_page: list[tuple[set[int], dict[str, Any]]] = [
        (set(int(p) for p in t.get("pages") or []), t) for t in truth_docs]

    # A wrongly excluded document is one that LOST an extractable truth document:
    # it overlaps that truth document and no other Setu document carries it. A
    # page-1 exclusion that sits beside the right document for that page (T02/T03)
    # is not a loss.
    wrong_excluded: list[SetuDoc] = []
    for doc in excluded_docs:
        for pages, t in truth_by_page:
            if not (doc.pages & pages) or t.get("extract") is not True:
                continue
            owner = matched.get(t["id"])
            if owner == doc.document_id or owner is None:
                wrong_excluded.append(doc)
                break

    swallowed: list[tuple[dict[str, Any], SetuDoc]] = []
    for entry in rows:
        if entry["matched"] is None:
            continue
        doc: SetuDoc = entry["doc"]
        for pages, t in truth_by_page:
            if t["id"] == entry["id"]:
                continue
            if t["id"] in matched:
                continue
            if pages and pages <= doc.pages:
                swallowed.append((t, doc))

    totals = {
        "truth_documents": len(truth_docs),
        "matched": len(matched),
        "pages_right": sum(1 for e in rows if e["pages_ok"]),
        "pages_right_or_acceptable": sum(1 for e in rows if e.get("pages_ok_acceptable")),
        "category_right": sum(1 for e in rows if e["category_ok"] is True),
        "category_checked": sum(1 for e in rows if e["category_ok"] is not None and e["matched"]),
        "missing": sum(1 for e in rows if e["matched"] is None),
        "extra_documents": len(extras),
        "wrongly_excluded": len(wrong_excluded),
        "wrongly_merged": len(swallowed),
    }
    return {"rows": rows, "extras": extras, "excluded": excluded_docs,
            "wrong_excluded": wrong_excluded, "swallowed": swallowed, "totals": totals}


def specific_checks(truth: dict[str, Any], setu: dict[str, Any], triage: dict[str, Any]) -> list[dict[str, Any]]:
    """The named checks the task asks for, read straight off the Setu rows."""
    docs: dict[int, SetuDoc] = setu["docs"]
    pages = setu["pages"]
    out: list[dict[str, Any]] = []

    def docs_on(page: int) -> list[SetuDoc]:
        return [d for d in docs.values() if page in d.pages]

    def verdict(ok: bool, text: str) -> dict[str, Any]:
        return {"ok": ok, "text": text}

    # pp7-9
    covering = [d for d in docs.values() if d.pages & {7, 8, 9}]
    if not covering:
        out.append(verdict(False, "pp7-9: no Setu document covers these pages"))
    else:
        parts = []
        ok = True
        for d in sorted(covering, key=lambda x: x.ordinal):
            parts.append("doc %d pp%s %s%s" % (d.document_id, sorted(d.pages), d.category,
                                               " (excluded)" if d.excluded else ""))
            if d.category != "supporting" and not d.excluded:
                ok = False
        out.append(verdict(ok, "pp7-9 category (want supporting, not extracted): " + "; ".join(parts)))

    # pp10-11
    airtel = [d for d in docs.values() if d.pages & {10, 11}]
    if len(airtel) == 1 and set(airtel[0].pages) >= {10, 11} and airtel[0].category == "tax_invoice":
        out.append(verdict(True, "pp10-11: one Setu document (doc %d) pp%s tax_invoice" %
                           (airtel[0].document_id, sorted(airtel[0].pages))))
    else:
        out.append(verdict(False, "pp10-11: want ONE tax_invoice; got " + (
            "none" if not airtel else "; ".join("doc %d pp%s %s" % (d.document_id, sorted(d.pages), d.category)
                                                for d in airtel))))

    # pp30-31
    card = [d for d in docs.values() if d.pages & {30, 31}]
    ok = len(card) == 1 and card[0].category == "supporting"
    out.append(verdict(ok, "pp30-31 category (want supporting): " + (
        "none" if not card else "; ".join("doc %d pp%s %s" % (d.document_id, sorted(d.pages), d.category)
                                          for d in card))))

    # page 1 split — the SPLIT is what is scored; the names are reported, and a
    # one-letter typo in a label (DTDC/DDTC) must not read as a split failure.
    on_one = sorted([d for d in docs.values() if 1 in d.pages], key=lambda x: x.ordinal)
    labels = " | ".join("%s(%s)" % (d.label or "?", d.category) for d in on_one)
    setu_labels = [d.label or "" for d in on_one]
    named = [s for s in ("Ajay", "Aggarwal", "DTDC") if _label_has_name(s, setu_labels)]
    near = [s for s in ("Ajay", "Aggarwal", "DTDC")
            if s not in named and _label_has_name(s, setu_labels, fuzzy=True)]
    out.append(verdict(len(on_one) >= 3,
                       "p1 documents: %d on page 1, named %s%s — %s" % (
                           len(on_one), ",".join(named) or "none",
                           " (near-miss %s)" % ",".join(near) if near else "", labels)))

    # KMAP 001-003 on pages 2-4
    kmap = {}
    problems = []
    for page, number in ((4, "001"), (3, "002"), (2, "003")):
        found = [d for d in docs.values() if page in d.pages]
        ok = len(found) == 1 and found[0].category == "tax_invoice" and number in (found[0].label or "")
        if not ok:
            problems.append("p%d -> %s" % (page, "; ".join("doc %d %s %s" % (d.document_id, d.category, d.label)
                                                           for d in found) or "nothing"))
        kmap[number] = found
    out.append(verdict(not problems, "KMAP/001-003 on pp2-4 as three tax invoices: " + (
        "yes (one each)" if not problems else "NO — " + " | ".join(problems))))

    # page 29 copy of 28
    page29 = pages.get(29) or {}
    duplicate_flag = page29.get("duplicate_of_page")
    holders = sorted([d for d in docs.values() if 29 in d.pages], key=lambda x: x.ordinal)
    excluded_29 = [d for d in holders if d.excluded]
    alone_29 = [d for d in holders if d.pages == {29} and not d.excluded]
    detail = "page 29 duplicate_of_page=%s" % (duplicate_flag if duplicate_flag is not None else "None")
    if duplicate_flag == 28:
        detail += "; the hash detector flagged it against page 28, as the key expects"
    if excluded_29:
        detail += "; EXCLUDED as a copy: doc %d (%s)" % (
            excluded_29[0].document_id, excluded_29[0].excluded_reason or "no reason")
    elif alone_29:
        detail += "; NOT excluded — page 29 is its own document (doc %d, %s)" % (
            alone_29[0].document_id, alone_29[0].category)
    elif holders:
        detail += "; NOT excluded — page 29 sits inside doc %s" % ", ".join(str(d.document_id) for d in holders)
    else:
        detail += "; no Setu document covers page 29"
    ok = bool(excluded_29) or (len(holders) == 1 and holders[0].pages >= {28, 29})
    out.append(verdict(ok, "p29 copy: " + detail))
    return out


# ---------------------------------------------------------------------------
# Extraction scoring
# ---------------------------------------------------------------------------


def _line_numbers(doc: SetuDoc) -> list[int]:
    numbers = set()
    for key, entry in doc.fields.items():
        match = re.match(r"line\.(\d+)\.", key)
        if match and str(entry.get("value") or "").strip():
            numbers.add(int(match.group(1)))
    return sorted(numbers)


def _phantom_lines(doc: SetuDoc, not_lines: Iterable[str]) -> list[str]:
    phantoms = []
    line_texts = []
    for key, entry in doc.fields.items():
        if re.match(r"line\.\d+\.(description|amount|hsn_sac)$", key) and str(entry.get("value") or "").strip():
            line_texts.append((key, str(entry["value"])))
    for item in not_lines or []:
        phrase = item
        quoted = re.findall(r"[\"']([^\"']+)[\"']", item)
        if quoted:
            phrase = quoted[0]
        else:
            phrase = re.split(r"\s+(?:is|are)\s+", item)[0]
        wanted = _tokens(phrase)
        if not wanted:
            continue
        for key, value in line_texts:
            have = _tokens(value)
            if have and len(wanted & have) / float(len(wanted)) >= 0.6:
                phantoms.append("line field %s = %r matches not_lines %r" % (key, value, item))
                break
    return phantoms


def _eval_arithmetic(text: str) -> tuple[Optional[bool], Optional[float], Optional[float]]:
    """(holds, lhs, rhs) for ``a x b = c`` style checks, else (None, None, None)."""
    if "=" not in text:
        return None, None, None
    lhs_text, rhs_text = text.rsplit("=", 1)
    rhs_match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", rhs_text)
    if not rhs_match:
        return None, None, None
    rhs = float(rhs_match.group(0).replace(",", ""))
    expr = lhs_text
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", expr)
    expr = expr.replace("×", "*").replace("x", "*").replace("X", "*")
    expr = re.sub(r"[^0-9+\-*/().]", "", expr)
    if not re.search(r"\d", expr):
        return None, None, None
    try:
        lhs = eval(expr, {"__builtins__": {}}, {})  # digits and + - * / ( ) only
    except Exception:
        return None, None, None
    return abs(float(lhs) - rhs) <= 0.02, float(lhs), rhs


def _check_operands(text: str, key_values: set[float], setu_numbers: set[float]) -> list[float]:
    """Numbers the key used that Setu never read (only where the key has them)."""
    missing = []
    for raw in re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", text):
        if raw.endswith("%") or re.search(re.escape(raw) + r"\s*%", text):
            continue
        number = norm_number(raw)
        if number is None or number in (0.0,):
            continue
        if abs(number) <= 30 and float(number).is_integer():
            continue  # rates and month/day multipliers, not amounts
        if not any(abs(number - k) <= 0.02 for k in key_values):
            continue
        if any(abs(number - s) <= 0.02 for s in setu_numbers):
            continue
        missing.append(number)
    return missing


def _tax_type_expected(text: str) -> Optional[str]:
    lowered = text.casefold()
    if "igst" in lowered and "inter-state" in lowered:
        return "igst"
    if "cgst" in lowered and ("intra-state" in lowered or "sgst" in lowered):
        return "cgst_sgst"
    return None


def score_extraction(truth: dict[str, Any], setu: dict[str, Any], triage: dict[str, Any]) -> dict[str, Any]:
    docs: dict[int, SetuDoc] = setu["docs"]
    documents: list[dict[str, Any]] = []
    hard_failures: list[str] = []
    handwriting_warnings: list[str] = []
    totals = {"fields": 0, "right": 0, "wrong": 0, "empty": 0, "extra": 0,
              "documents": 0, "documents_with_wrong": 0}

    for entry in triage["rows"]:
        t = next((x for x in truth["documents"] if x["id"] == entry["id"]), None)
        if t is None or entry["matched"] is None:
            continue
        if t.get("expected_category") not in ("tax_invoice", "no_gst_expense"):
            continue
        if t.get("extract") is not True:
            continue
        doc: SetuDoc = entry["doc"]
        key_fields: dict[str, Any] = t.get("fields") or {}
        report: dict[str, Any] = {
            "truth": t["id"], "document_id": doc.document_id, "category": doc.category,
            "extraction_status": doc.extraction_status, "skipped_reason": doc.skipped_reason,
            "fields": [], "extras": [], "checks": [], "hard_failures": [],
            "phantom_lines": [], "lines": {},
        }

        if doc.extraction_status not in ("complete", "partial"):
            note = "not extracted (%s)" % (doc.skipped_reason or doc.extraction_status or "no extraction row")
            report["hard_failures"].append(note)
            hard_failures.append("%s doc %d: %s" % (t["id"], doc.document_id, note))
            report["fields"].append({"field": "(document)", "status": "no_extraction", "detail": note})
            documents.append(report)
            totals["documents"] += 1
            totals["documents_with_wrong"] += 1
            continue

        for key, spec in key_fields.items():
            expected = spec.get("value") if isinstance(spec, dict) else spec
            source = (spec or {}).get("source") if isinstance(spec, dict) else None
            actual = doc.value(key)
            actual_empty = doc.empty(key)
            totals["fields"] += 1
            if expected is None:
                status = "right" if actual_empty else "wrong"
                detail = "key expects empty" + ("" if actual_empty else "; Setu read %r" % actual)
            elif source == "handwritten" and actual_empty:
                status = "right"
                detail = "handwritten in the key; empty counts as correct"
            elif actual_empty:
                status = "empty"
                detail = "Setu left it blank (key: %r, %s)" % (expected, source)
            elif values_equal(key, expected, actual):
                status = "right"
                detail = "%r == %r" % (actual, expected)
            else:
                status = "wrong"
                detail = "Setu %r vs key %r (%s)" % (actual, expected, source)
            if status == "right":
                totals["right"] += 1
            elif status == "wrong":
                totals["wrong"] += 1
            elif status == "empty":
                totals["empty"] += 1
            report["fields"].append({"field": key, "status": status, "expected": expected,
                                     "actual": actual, "source": source, "detail": detail})
            # handwriting rule: a handwritten key value must not be Setu's value
            if source == "handwritten" and not actual_empty and values_equal(key, expected, actual):
                message = "%s doc %d: field %s carries the HANDWRITTEN value %r" % (
                    t["id"], doc.document_id, key, actual)
                handwriting_warnings.append(message)

        # extras: Setu read something the key does not list
        for key, entry_field in doc.fields.items():
            if key in key_fields:
                continue
            if not str(entry_field.get("value") or "").strip():
                continue
            report["extras"].append({"field": key, "value": entry_field.get("value")})
            totals["extra"] += 1

        # handwritten figures that must never be values
        guard = set(t.get("handwritten_must_not_be_values") or [])
        if guard:
            for key, entry_field in doc.fields.items():
                value = entry_field.get("value")
                number = norm_number(value)
                if number is None:
                    continue
                for forbidden in guard:
                    if abs(number - float(forbidden)) <= 0.05:
                        message = "%s doc %d: field %s = %r is a handwritten figure that must not be a value" % (
                            t["id"], doc.document_id, key, value)
                        report["hard_failures"].append(message)
                        hard_failures.append(message)
                        break

        # line counts + phantom lines
        lines_expected = t.get("lines_expected")
        setu_lines = _line_numbers(doc)
        if lines_expected is not None:
            report["lines"] = {"expected": lines_expected, "setu": len(setu_lines), "numbers": setu_lines,
                               "ok": len(setu_lines) == int(lines_expected)}
        report["phantom_lines"] = _phantom_lines(doc, t.get("not_lines") or [])

        # arithmetic + tax-type checks
        setu_numbers = doc.numbers()
        key_numbers = set()
        for spec in key_fields.values():
            if isinstance(spec, dict):
                number = norm_number(spec.get("value"))
                if number is not None:
                    key_numbers.add(number)
        for text in t.get("checks") or []:
            holds, lhs, rhs = _eval_arithmetic(str(text))
            if holds is None:
                wanted = _tax_type_expected(str(text))
                if wanted == "igst":
                    ok = not doc.empty("igst_amount") and doc.empty("cgst_amount") and doc.empty("sgst_amount")
                    report["checks"].append({"text": text, "status": "pass" if ok else "fail",
                                             "detail": "Setu igst=%r cgst=%r sgst=%r" % (
                                                 doc.value("igst_amount"), doc.value("cgst_amount"), doc.value("sgst_amount"))})
                elif wanted == "cgst_sgst":
                    ok = not doc.empty("cgst_amount") and not doc.empty("sgst_amount") and doc.empty("igst_amount")
                    report["checks"].append({"text": text, "status": "pass" if ok else "fail",
                                             "detail": "Setu cgst=%r sgst=%r igst=%r" % (
                                                 doc.value("cgst_amount"), doc.value("sgst_amount"), doc.value("igst_amount"))})
                else:
                    report["checks"].append({"text": text, "status": "note", "detail": "not arithmetic"})
                continue
            missing = _check_operands(str(text), key_numbers, setu_numbers)
            if missing:
                report["checks"].append({
                    "text": text, "status": "operand_not_read",
                    "detail": "arithmetic holds (%s); Setu never read %s" % (holds, sorted(set(missing)))})
            else:
                report["checks"].append({
                    "text": text, "status": "pass" if holds else "fail",
                    "detail": "lhs=%s rhs=%s" % (lhs, rhs)})

        if report["hard_failures"] or report["phantom_lines"]:
            totals["documents_with_wrong"] += 1
        totals["documents"] += 1
        documents.append(report)

    totals["accuracy"] = (totals["right"] / totals["fields"]) if totals["fields"] else 0.0
    return {"documents": documents, "hard_failures": hard_failures,
            "handwriting_warnings": handwriting_warnings, "totals": totals}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_detail(truth: dict[str, Any], setu: dict[str, Any], triage: dict[str, Any],
                  extraction: dict[str, Any], checks: list[dict[str, Any]], db_path: str) -> str:
    lines: list[str] = []
    add = lines.append
    add("Setu vs answer key — upload %s" % setu["upload_id"])
    add("database %s (read-only)" % db_path)
    add("key      %s  (%s, %s pages, %s truth documents)" % (
        truth.get("meta", {}).get("name", "?"), truth.get("meta", {}).get("pdf", "?"),
        truth.get("meta", {}).get("pages_total", "?"), len(truth["documents"])))
    t = triage["totals"]
    add("")
    add("TRIAGE TOTALS")
    add("  truth documents %d | matched %d | pages right %d (or %d accepting the key's allowed excluded pages) | "
        "category right %d/%d | missing %d | extra Setu docs %d | wrongly excluded %d | wrongly merged %d" % (
            t["truth_documents"], t["matched"], t["pages_right"], t["pages_right_or_acceptable"],
            t["category_right"], t["category_checked"],
            t["missing"], t["extra_documents"], t["wrongly_excluded"], t["wrongly_merged"]))
    add("")
    add("NAMED CHECKS (the cases this key calls out)")
    for check in checks:
        add(("  OK  " if check["ok"] else "  BAD ") + check["text"])
    add("")
    add("TRIAGE PER TRUTH DOCUMENT")
    for row in triage["rows"]:
        doc: Optional[SetuDoc] = row["doc"]
        add("  %s pp%s -> %s pages %s category %s%s%s" % (
            row["id"], row["pages"],
            "doc %d pp%s" % (doc.document_id, sorted(doc.pages)) if doc else "NOT FOUND",
            "OK" if row["pages_ok"] else "WRONG",
            "%s vs key %s %s" % (doc.category, row["expected"], "OK" if row["category_ok"] else
                                 ("CA" if row["category_ok"] is None else "WRONG")) if doc else "-",
            " excluded=%s" % (doc.excluded_reason or "yes") if doc and doc.excluded else "",
            (" — " + "; ".join(row["notes"])) if row["notes"] else ""))
    add("")
    add("EXCLUDED SETU DOCUMENTS (listed separately)")
    if not triage["excluded"]:
        add("  none")
    for doc in sorted(triage["excluded"], key=lambda d: d.ordinal):
        add("  doc %d pp%s %s — %s (flags %s)" % (doc.document_id, sorted(doc.pages), doc.category,
                                                  doc.excluded_reason or "no reason", doc.flags))
    add("")
    add("EXTRA SETU DOCUMENTS (no truth match)")
    if not triage["extras"]:
        add("  none")
    for doc in sorted(triage["extras"], key=lambda d: d.ordinal):
        add("  doc %d pp%s %s label %r flags %s" % (doc.document_id, sorted(doc.pages), doc.category,
                                                    doc.label, doc.flags))
    add("")
    add("WRONGLY EXCLUDED (an extractable truth document lost to an exclusion)")
    if not triage["wrong_excluded"]:
        add("  none")
    for doc in triage["wrong_excluded"]:
        add("  doc %d pp%s %s — %s" % (doc.document_id, sorted(doc.pages), doc.category,
                                       doc.excluded_reason or "no reason"))
    add("")
    add("WRONGLY MERGED (an unmatched truth document swallowed inside another)")
    if not triage["swallowed"]:
        add("  none")
    for truth_doc, doc in triage["swallowed"]:
        add("  %s pp%s sits inside doc %d pp%s" % (truth_doc["id"], truth_doc.get("pages"),
                                                   doc.document_id, sorted(doc.pages)))

    add("")
    add("PAGE ROWS (duplicate / blank detection)")
    for page in sorted(setu["pages"]):
        row = setu["pages"][page]
        add("  p%-3d status=%s duplicate_of_page=%s" % (page, row.get("status"), row.get("duplicate_of_page")))

    add("")
    add("EXTRACTION — per document")
    totals = extraction["totals"]
    add("  fields scored %d: right %d, wrong %d, empty %d, extra %d (accuracy %.1f%%)" % (
        totals["fields"], totals["right"], totals["wrong"], totals["empty"], totals["extra"],
        100.0 * totals["accuracy"]))
    for report in extraction["documents"]:
        add("")
        add("  %s doc %d (%s, extraction %s%s) — fields right %d/%d" % (
            report["truth"], report["document_id"], report["category"], report["extraction_status"],
            ", " + report["skipped_reason"] if report.get("skipped_reason") else "",
            sum(1 for f in report["fields"] if f["status"] == "right"), len(report["fields"])))
        for field in report["fields"]:
            add("    %-24s %-6s %s" % (field["field"], field["status"], field.get("detail", "")))
        if report["extras"]:
            add("    extras: " + ", ".join("%s=%r" % (e["field"], e["value"]) for e in report["extras"]))
        if report["lines"]:
            add("    lines: key %s vs Setu %s (%s)" % (report["lines"]["expected"], report["lines"]["setu"],
                                                       "OK" if report["lines"]["ok"] else "MISMATCH"))
        for phantom in report["phantom_lines"]:
            add("    NOT_LINES: " + phantom)
        for check in report["checks"]:
            add("    check[%s] %s — %s" % (check["status"], check["text"], check["detail"]))
        for failure in report["hard_failures"]:
            add("    HARD FAILURE: " + failure)

    add("")
    add("HARD FAILURES")
    if not extraction["hard_failures"]:
        add("  none")
    for item in extraction["hard_failures"]:
        add("  " + item)
    add("")
    add("HANDWRITING-RULE WARNINGS (handwritten key value used as a Setu value)")
    if not extraction["handwriting_warnings"]:
        add("  none")
    for item in extraction["handwriting_warnings"]:
        add("  " + item)

    add("")
    add("SETU'S OWN CHECKS (as stored) and the document log")
    for doc in sorted(setu["docs"].values(), key=lambda d: d.ordinal):
        for check in doc.checks:
            if check["status"] != "pass":
                add("  doc %d %s: %s — %s" % (doc.document_id, check["key"], check["status"], check["detail"]))
    for entry in setu["log"]:
        if entry["in_upload"]:
            add("  log doc %s %s %s" % (entry["document_id"], entry["action"], entry["after_json"]))

    add("")
    add("OPEN CA QUESTIONS (from the key — context, not scored)")
    for item in truth.get("open_ca_questions") or []:
        add("  " + item)
    add("")
    add("PAYMENT LINKS (from the key — context for annotations, not scored)")
    for link in truth.get("payment_links") or []:
        add("  cheque %s dt %s %s pays %s [%s]" % (
            link.get("cheque"), link.get("date"), link.get("amount"), link.get("pays"), link.get("source")))
    return "\n".join(lines) + "\n"


def render_summary(truth: dict[str, Any], setu: dict[str, Any], triage: dict[str, Any],
                   extraction: dict[str, Any], checks: list[dict[str, Any]], out_path: str) -> list[str]:
    t = triage["totals"]
    e = extraction["totals"]
    lines: list[str] = []
    lines.append("Setu vs key — upload %s, %s" % (setu["upload_id"], truth.get("meta", {}).get("pdf", "?")))
    lines.append("Triage: %d truth docs, matched %d, pages right %d (or %d with the key's allowed excluded "
                 "pages), category right %d/%d, missing %d, extra %d, wrongly excluded %d, wrongly merged %d" % (
                     t["truth_documents"], t["matched"], t["pages_right"], t["pages_right_or_acceptable"],
                     t["category_right"], t["category_checked"], t["missing"], t["extra_documents"],
                     t["wrongly_excluded"], t["wrongly_merged"]))
    for check in checks:
        lines.append(("  OK  " if check["ok"] else "  BAD ") + check["text"])
    lines.append("Extraction: %d fields on %d documents — right %d, wrong %d, empty %d, extra %d "
                 "(%.1f%% right)" % (e["fields"], e["documents"], e["right"], e["wrong"], e["empty"],
                                     e["extra"], 100.0 * e["accuracy"]))
    per_doc: list[str] = []
    for report in extraction["documents"]:
        right = sum(1 for f in report["fields"] if f["status"] == "right")
        per_doc.append("  %s doc %d: %d/%d right%s" % (
            report["truth"], report["document_id"], right, len(report["fields"]),
            " +%d extra" % len(report["extras"]) if report["extras"] else ""))
    lines.append("Extraction per document:")
    lines.extend(per_doc)
    if extraction["hard_failures"]:
        lines.append("HARD FAILURES (%d):" % len(extraction["hard_failures"]))
        for item in extraction["hard_failures"]:
            lines.append("  " + item)
    else:
        lines.append("HARD FAILURES: none")
    if extraction["handwriting_warnings"]:
        lines.append("handwriting warnings: %d (see report)" % len(extraction["handwriting_warnings"]))
    lines.append("Full report: %s" % out_path)

    if len(lines) > 25:
        head = lines[:3 + len(checks) + 2]
        tail = lines[-1:]
        keep = 25 - len(head) - len(tail) - 1
        if keep < 1:
            keep = 1
        lines = head + per_doc[:keep] + ["  ... %d more document lines in the report" % max(0, len(per_doc) - keep)] + tail
    return lines[:25]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(db_path: str, upload_id: int, truth_path: str, out_path: str) -> tuple[list[str], str]:
    con = open_read_only(db_path)
    try:
        schema = discover_schema(con)
        schema["columns"] = {name: _columns(con, name) for name in schema.values()}
        setu = load_setu(con, schema, upload_id)
    finally:
        con.close()
    truth = load_truth(truth_path)
    triage = score_triage(truth, setu)
    checks = specific_checks(truth, setu, triage)
    extraction = score_extraction(truth, setu, triage)
    detail = render_detail(truth, setu, triage, extraction, checks, db_path)
    summary = render_summary(truth, setu, triage, extraction, checks, out_path)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(detail)
    return summary, out_path


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Score Setu's triage/extraction against the answer key (read-only).")
    parser.add_argument("--db", required=True, help="path to a Setu database COPY (opened read-only)")
    parser.add_argument("--upload", required=True, type=int, help="invoice upload id")
    parser.add_argument("--truth", required=True, help="path to the ground-truth JSON")
    parser.add_argument("--out", default=None, help="report path (default: score_<upload>.txt beside the database)")
    args = parser.parse_args(argv)
    out_path = args.out or os.path.join(os.path.dirname(os.path.abspath(args.db)), "score_%d.txt" % args.upload)
    summary, out_path = run(args.db, args.upload, args.truth, out_path)
    for line in summary:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
