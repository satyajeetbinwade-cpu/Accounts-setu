"""Phase-4 confidence: an evidence-based 0-100 score per stored field.

ROLES — READ THIS FIRST
-----------------------
* **The model READS values.** It is asked to transcribe what is printed and to
  say where on the page each value sits. It is NEVER asked how sure it is.
* **The app SCORES them.** The score is computed here, from evidence the app
  already holds: whether two independent reads agreed, whether the value passes
  its own format rule, whether the deterministic checks passed, whether the
  PDF's own text layer contains the value, and whether the value is a
  transcription of handwriting.

Nothing in this module makes a model self-report a confidence number. A model's
own "I am 95% sure" is a claim, not evidence, and it cannot be calibrated against
anything — which is the entire point of the corrections table this module
writes.

WHY IT CAN BE CALIBRATED
------------------------
``record_correction`` logs EVERY human decision, including "confirmed as is".
The confirmations are what let ``calibration.py`` measure whether the 90-band
really is right 90% of the time, and the edits are what show which bands are
lying. A score nobody can check is a decoration; this one is an input to a
measurement.

FORMULA v1 — A PURE FUNCTION
----------------------------
Same inputs always give the same score. Every weight and cap comes from
settings (see :func:`load_config`), so the numbers can be tuned without a code
change, and every signal and cap that fired is stored beside the score so a
reviewer can see WHY rather than being told.

    base                                    +40
    independent read agreed (exact)         +30
    independent read agreed (near, text)    +20
    format rule passes / no rule applies    +15
    involved checks pass / none involve it  +10
    value found in the PDF text layer        +5
    ---------------------------------------------
    a perfect field                         100

CAPS (the LOWEST one wins):
    reads disagree                            60
    an involved ARITHMETIC check failed       60
    GSTIN checksum invalid                    50
    status out_of_region                      85
    a single reader only (no read B)          70
    status needs_printed / unreadable          0   (needs you; also the
                                                     handwritten-monetary cap of 80)
    a handwritten MONETARY value              80

NO SCORE BY RULE (the score is NULL, and the signals say why):
    * ``not_printed`` on an OPTIONAL field — nothing was printed, so there is no
      read to have confidence in;
    * ``not_printed`` on a REQUIRED field — that is a missing-required item for
      Phase 3 to ask about, not a low-confidence value;
    * a DERIVED value — it is calculated, not read, and carries a "derived" tag.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from src.invoice_extract import fields_v2 as fv2
from src.invoice_extract import gst_identifiers as gst_ids
from src.invoice_extract import immutable as immutable_mod
from src.invoice_extract import normalisers as nz
from src.invoice_extract.confidence_schema import (
    ACTION_EDITED,
    ACTIONS,
    REASON_CODES,
)
from src.invoice_extract.rules_schema import (
    CHECK_FAIL,
    CHECK_PASS,
    LEVEL_REQUIRED,
)
from src.invoice_extract.service import InvoiceExtractError, _connect

FORMULA_VERSION = "v1"

#: Re-exported for callers that already import it from here. The rule itself
#: lives in exactly one place — ``src/invoice_extract/immutable.py`` — which the
#: legacy service and the review screen consult too.
IMMUTABLE_UPLOAD_STATUSES = immutable_mod.IMMUTABLE_UPLOAD_STATUSES
is_immutable_status = immutable_mod.is_immutable_status

# --- settings keys --------------------------------------------------------
KEY_THRESHOLD = "invoice_extract.review_threshold"
KEY_FORMULA_VERSION = "invoice_extract.score.formula_version"
KEY_BULK_ENABLED = "invoice_extract.bulk_approve_enabled"
KEY_BULK_MIN_FIELDS = "invoice_extract.bulk_approve.min_fields"
KEY_BULK_MAX_EDIT_RATE = "invoice_extract.bulk_approve.max_edit_rate_pct"
KEY_BULK_MIN_MONTHS = "invoice_extract.bulk_approve.min_months"

#: The weights, as settings. Defaults are the formula-v1 numbers.
WEIGHT_DEFAULTS: dict[str, float] = {
    "invoice_extract.score.base": 40,
    "invoice_extract.score.read.exact": 30,
    "invoice_extract.score.read.near": 20,
    "invoice_extract.score.format.pass": 15,
    "invoice_extract.score.format.no_rule": 15,
    "invoice_extract.score.cross.pass": 10,
    "invoice_extract.score.cross.none": 10,
    "invoice_extract.score.text_layer": 5,
    "invoice_extract.score.cap.disagree": 60,
    "invoice_extract.score.cap.arithmetic_fail": 60,
    "invoice_extract.score.cap.gstin_invalid": 50,
    "invoice_extract.score.cap.out_of_region": 85,
    "invoice_extract.score.cap.single_reader": 70,
    "invoice_extract.score.cap.handwritten_monetary": 80,
    "invoice_extract.score.cap.needs_printed": 0,
}

#: Checks whose failure is an ARITHMETIC problem (they cap at 60). The other
#: checks still zero the cross-check signal, but they do not cap — a failed
#: direction expectation is not evidence that a NUMBER is wrong.
ARITHMETIC_CHECKS = frozenset({
    "totals_reconcile",
    "tax_equals_rate_times_taxable",
    "derived_rate_on_valid_slab",
})

#: Which fields each check is ABOUT, whether it passes or fails.
#:
#: This has to be static for the pass case: Phase 3 stores the fields a FAILURE
#: implicates, and a passing check implicates nothing. ``"*"`` means every
#: value-bearing field; ``"line.*.amount"`` means that field on every line.
CHECK_INVOLVES: dict[str, tuple[str, ...]] = {
    "totals_reconcile": (
        "invoice_total", "taxable_value", "round_off", "total_tax",
        "cgst_amount", "sgst_amount", "igst_amount", "line.*.amount",
    ),
    "tax_equals_rate_times_taxable": (
        "taxable_value", "cgst_rate", "sgst_rate", "igst_rate",
        "cgst_amount", "sgst_amount", "igst_amount", "line.*.amount",
    ),
    "derived_rate_on_valid_slab": (
        "taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "line.*.amount",
    ),
    "gstin_valid": ("issuer_gstin", "recipient_gstin"),
    "state_code_matches_gstin": ("place_of_supply", "issuer_gstin", "recipient_gstin"),
    "expected_tax_type": (
        "place_of_supply", "issuer_gstin", "cgst_amount", "sgst_amount", "igst_amount",
    ),
    # These two are per-field by nature; their involvement comes from what the
    # check actually flagged (recorded in fields_implicated_json).
    "provenance_in_region": (),
    "handwriting_guard": (),
}

#: Fields with a FORM validation rule. Everything else scores the "no rule" 15.
#: (A rule that fails is 0 — the check exists to catch a value that cannot be
#: what it claims to be.) The rules themselves live in ``validation_result``.

#: The number of digits a valid HSN (goods) / SAC (services) code has.
HSN_LENGTHS = (4, 6, 8)


@dataclass(frozen=True)
class ScoreConfig:
    """Every number the formula uses, resolved for one client."""

    threshold: float = 90.0
    formula_version: str = FORMULA_VERSION
    weights: dict[str, float] = field(default_factory=lambda: dict(WEIGHT_DEFAULTS))

    def weight(self, key: str) -> float:
        return float(self.weights.get(key, WEIGHT_DEFAULTS[key]))


def _setting_raw(key: str, *, client_id: Optional[int], db_path) -> Optional[str]:
    """A settings value, resolved in this order:

    1. a per-client C1 rule override (C1 is the codebase's firm-default-with-
       per-client-override store, and the one the screen already edits),
    2. the firm-wide C1 rule,
    3. the flat C3 settings store (so a value set on a settings screen wins over
       a code default),
    4. the code default.

    Precedence is deliberate and one-directional: the narrower scope always wins.
    """
    from src.rules import service as rules_service

    try:
        value = rules_service.get_effective_value(key, client_id=client_id, db_path=db_path)
    except Exception:  # noqa: BLE001 — C1 must never block a score
        value = None
    if value is not None and str(value).strip() != "":
        return str(value)
    try:
        from src.settings import service as settings_service

        raw = settings_service.get_setting_str(key, default=None, db_path=db_path)
    except Exception:  # noqa: BLE001
        raw = None
    return None if raw is None or str(raw).strip() == "" else str(raw)


def load_config(*, client_id: Optional[int] = None, db_path=None) -> ScoreConfig:
    """The formula's numbers, resolved for one client."""
    def number(key: str, default: float) -> float:
        raw = _setting_raw(key, client_id=client_id, db_path=db_path)
        if raw is None:
            return default
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            return default

    version = _setting_raw(KEY_FORMULA_VERSION, client_id=client_id, db_path=db_path)
    weights = {key: number(key, default) for key, default in WEIGHT_DEFAULTS.items()}
    return ScoreConfig(
        threshold=number(KEY_THRESHOLD, 90.0),
        formula_version=(str(version).strip() if version else FORMULA_VERSION),
        weights=weights,
    )


# ---------------------------------------------------------------------------
# The pure formula
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    name: str
    points: float
    reason: str

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "points": self.points, "reason": self.reason}


@dataclass(frozen=True)
class Cap:
    name: str
    cap: float
    reason: str

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "cap": self.cap, "reason": self.reason}


@dataclass(frozen=True)
class FieldScore:
    """The score for one field. ``score`` is None when the field has none BY RULE."""

    field_id: int
    score: Optional[float]
    signals: tuple[Signal, ...]
    caps: tuple[Cap, ...]
    formula_version: str
    threshold_used: float

    @property
    def below_threshold(self) -> bool:
        return self.score is not None and self.score < self.threshold_used

    @property
    def flagged(self) -> bool:
        """Is this field something a reviewer must look at?"""
        return self.score is None or self.below_threshold


# ---------------------------------------------------------------------------
# Field-level validation rules (the +15 "format validation" signal)
# ---------------------------------------------------------------------------


def validation_result(
    field_key: str, value: Optional[str], *, known_state_codes: Optional[set[str]] = None,
) -> Optional[tuple[bool, str]]:
    """(passes, reason) for a field that HAS a format rule, else None.

    ``None`` means "this field has no format rule" — which scores the same 15 as
    a rule that passes, because the formula is not allowed to punish a field for
    the app not knowing how to validate it.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    kind = fv2.kind_of(field_key)
    suffix = field_key.split(".")[-1] if field_key.startswith("line.") else field_key

    if suffix in ("issuer_gstin", "recipient_gstin"):
        result = gst_ids.gstin_valid(text, known_state_codes=known_state_codes)
        return result.ok, result.reason or "the GSTIN is structurally valid"
    if suffix in ("issuer_pan", "recipient_pan"):
        ok = gst_ids.pan_valid(text)
        return ok, "" if ok else "not PAN-shaped (5 letters, 4 digits, 1 letter)"
    if suffix == "invoice_date":
        ok = nz.normalise_date(text) is not None
        return ok, "" if ok else "not a readable date"
    if suffix in ("cgst_rate", "sgst_rate", "igst_rate"):
        number = nz.normalise_number(text)
        if number is None:
            return False, "not a number"
        return 0.0 <= float(number) <= 100.0, "" if 0.0 <= float(number) <= 100.0 else "outside 0-100"
    if suffix in ("hsn_sac",):
        digits = "".join(ch for ch in text if ch.isdigit())
        ok = len(digits) in HSN_LENGTHS and digits == text.replace(" ", "")
        return ok, "" if ok else f"an HSN/SAC code has {HSN_LENGTHS} digits"
    if kind == fv2.KIND_NUMBER:
        ok = nz.normalise_number(text) is not None
        return ok, "" if ok else "not a number"
    return None


def _match_field_pattern(pattern: str, field_key: str) -> bool:
    if pattern == "*":
        return True
    if pattern.startswith("line.*."):
        suffix = pattern.split(".")[-1]
        parsed = fv2.parse_line_key(field_key)
        return parsed is not None and parsed[1] == suffix
    return pattern == field_key


# ---------------------------------------------------------------------------
# Scoring one field
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldFacts:
    """Everything the formula is allowed to look at for one field."""

    field_id: int
    field_key: str
    status: str
    reads_agree: Optional[str]
    text_layer_match: Optional[int]
    is_handwritten: bool
    in_region: bool
    value: Optional[str]
    value_a: Optional[str]
    value_b: Optional[str]
    #: True when read B did not run at all for this document (single reader).
    single_reader: bool
    #: checks that involve this field: check_key -> status
    involved_checks: dict[str, str]
    #: every check status of the document, for the "no check involves it" case
    check_count: int
    required: bool


def score_field(facts: FieldFacts, config: ScoreConfig, *, known_state_codes=None) -> FieldScore:
    """THE formula. A pure function of ``facts`` and ``config``."""
    w = config.weight
    signals: list[Signal] = []
    caps: list[Cap] = []

    # ---- no score BY RULE -------------------------------------------------
    if facts.status == fv2.STATUS_NOT_PRINTED or facts.value is None:
        if facts.required:
            reason = (
                "the field is required on this document and was not printed — that is a "
                "missing-required item, not a low-confidence value"
            )
        else:
            reason = "the document does not print this field, so there is no read to score"
        return FieldScore(
            facts.field_id, None,
            (Signal("no_score", 0, reason),), (), config.formula_version, config.threshold,
        )

    # ---- base ------------------------------------------------------------
    base = w("invoice_extract.score.base")
    signals.append(Signal("base", base, "every read field starts here"))

    # ---- independent read ------------------------------------------------
    agree = facts.reads_agree
    if agree == nz.AGREE_EXACT:
        points = w("invoice_extract.score.read.exact")
        reason = "two independent reads produced the same value"
    elif agree == nz.AGREE_NEAR:
        kind = fv2.kind_of(facts.field_key)
        if kind == fv2.KIND_TEXT:
            points = w("invoice_extract.score.read.near")
            reason = "two independent reads produced nearly the same text"
        else:
            points = 0.0
            reason = (
                f"the reads differed on a {kind} field — 'near' is never allowed for numbers, "
                "dates or identifiers"
            )
    elif agree == nz.AGREE_MISSING_B:
        points = 0.0
        reason = "only one read produced a value"
    else:
        points = 0.0
        reason = f"the two reads did not agree ({agree or 'no comparison was stored'})"
    signals.append(Signal("independent_read", points, reason))

    # ---- format validation ----------------------------------------------
    validation = validation_result(
        facts.field_key, facts.value, known_state_codes=known_state_codes,
    )
    if validation is None:
        points = w("invoice_extract.score.format.no_rule")
        signals.append(Signal("format", points, "no format rule applies to this field"))
        rules_failed = False
    else:
        ok, why = validation
        points = w("invoice_extract.score.format.pass") if ok else 0.0
        signals.append(Signal(
            "format", points,
            "the value passes its format rule" if ok else f"the value fails its format rule: {why}",
        ))
        rules_failed = not ok

    # ---- cross-check -----------------------------------------------------
    failed = [key for key, status in facts.involved_checks.items() if status == CHECK_FAIL]
    passed = [key for key, status in facts.involved_checks.items() if status == CHECK_PASS]
    if failed:
        points = 0.0
        reason = "a check involving this field failed: " + ", ".join(sorted(failed))
    elif passed:
        points = w("invoice_extract.score.cross.pass")
        reason = "every check involving this field passed: " + ", ".join(sorted(passed))
    elif facts.involved_checks:
        points = w("invoice_extract.score.cross.none")
        reason = (
            "the checks involving this field could not run (not applicable), which is no "
            "evidence about this value either way"
        )
    else:
        points = w("invoice_extract.score.cross.none")
        reason = "no check involves this field"
    signals.append(Signal("cross_check", points, reason))

    # ---- text layer ------------------------------------------------------
    if facts.text_layer_match == 1:
        points = w("invoice_extract.score.text_layer")
        signals.append(Signal(
            "text_layer", points, "the PDF's own text layer contains this value inside its box",
        ))
    else:
        signals.append(Signal(
            "text_layer", 0.0,
            "no text-layer match" if facts.text_layer_match == 0
            else "the source has no text layer to match against (a scan or an image)",
        ))

    # ---- caps ------------------------------------------------------------
    if agree == nz.AGREE_DISAGREE:
        caps.append(Cap(
            "reads_disagree", w("invoice_extract.score.cap.disagree"),
            "the two independent reads disagree — a person must choose",
        ))
    arith_failed = sorted(key for key in failed if key in ARITHMETIC_CHECKS)
    if arith_failed:
        caps.append(Cap(
            "arithmetic_check_failed", w("invoice_extract.score.cap.arithmetic_fail"),
            "an arithmetic check involving this field failed: " + ", ".join(arith_failed),
        ))
    if rules_failed and facts.field_key.split(".")[-1].endswith("gstin"):
        caps.append(Cap(
            "gstin_checksum_invalid", w("invoice_extract.score.cap.gstin_invalid"),
            "the GSTIN's check character does not match its own first 14 characters",
        ))
    if facts.status == fv2.STATUS_OUT_OF_REGION or not facts.in_region:
        caps.append(Cap(
            "out_of_region", w("invoice_extract.score.cap.out_of_region"),
            "the value's box lies outside the document's own regions",
        ))
    if facts.single_reader:
        caps.append(Cap(
            "single_reader", w("invoice_extract.score.cap.single_reader"),
            "only one reader ran for this document, so there is no independent second opinion",
        ))
    if facts.status in (fv2.STATUS_NEEDS_PRINTED, fv2.STATUS_UNREADABLE):
        caps.append(Cap(
            "needs_printed", w("invoice_extract.score.cap.needs_printed"),
            "the value is handwritten or illegible where a printed value is required",
        ))
    elif facts.is_handwritten and fv2.is_monetary(facts.field_key):
        caps.append(Cap(
            "handwritten_monetary", w("invoice_extract.score.cap.handwritten_monetary"),
            "a handwritten monetary value never scores above this, even when both reads agree",
        ))

    total = sum(signal.points for signal in signals)
    if caps:
        total = min(total, min(cap.cap for cap in caps))
    total = max(0.0, min(100.0, float(total)))
    return FieldScore(
        facts.field_id, round(total, 2), tuple(signals), tuple(caps),
        config.formula_version, config.threshold,
    )


# ---------------------------------------------------------------------------
# Gathering the facts out of the database
# ---------------------------------------------------------------------------


def _dict_row(cur: sqlite3.Cursor, row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {c[0]: row[i] for i, c in enumerate(cur.description)}


def _involved_checks(field_key: str, check_rows: list[dict[str, Any]]) -> dict[str, str]:
    """The checks that involve this field, with their status."""
    involved: dict[str, str] = {}
    for row in check_rows:
        key = str(row["check_key"])
        patterns = CHECK_INVOLVES.get(key)
        if patterns is None:
            continue
        recorded = json.loads(row.get("fields_implicated_json") or "[]")
        if any(_match_field_pattern(pattern, field_key) for pattern in patterns) \
                or field_key in recorded:
            involved[key] = str(row["status"])
    return involved


def score_extraction(extraction_id: int, *, db_path=None) -> dict[str, Any]:
    """Score every field of one extraction. Idempotent; safe to re-run.

    Re-running REPLACES the score rows for this extraction's fields (a score is
    a function of the evidence, and the evidence may have changed since — a
    Phase-3 override, a re-run of the rules). The corrections log is never
    touched: what a person decided, and the score they saw at the time, is
    history.
    """
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT e.extraction_id, e.document_id, e.provider_b, e.model_b, d.upload_id, "
            "u.client_id, d.category FROM invoice_doc_extractions e "
            "JOIN invoice_documents d ON d.document_id = e.document_id "
            "LEFT JOIN invoice_uploads u ON u.upload_id = d.upload_id "
            "WHERE e.extraction_id = ?",
            (extraction_id,),
        )
        extraction = _dict_row(cur, cur.fetchone())
        if extraction is None:
            raise InvoiceExtractError(f"Unknown extraction {extraction_id}.")
        document_id = int(extraction["document_id"])
        client_id = extraction.get("client_id")

        cur = conn.execute(
            "SELECT field_id, field_key, status, value_a, value_b, reads_agree, text_layer_match, "
            "is_handwritten, in_region FROM invoice_doc_fields WHERE extraction_id = ? "
            "ORDER BY field_id",
            (extraction_id,),
        )
        field_rows = [_dict_row(cur, row) for row in cur.fetchall()]

        cur = conn.execute(
            "SELECT check_key, status, fields_implicated_json FROM invoice_doc_checks "
            "WHERE extraction_id = ?",
            (extraction_id,),
        )
        check_rows = [_dict_row(cur, row) for row in cur.fetchall()]

        cur = conn.execute(
            "SELECT field_key, level FROM invoice_doc_applicability WHERE document_id = ?",
            (document_id,),
        )
        levels = {str(r["field_key"]): str(r["level"]) for r in
                  (_dict_row(cur, row) for row in cur.fetchall())}
    finally:
        conn.close()

    config = load_config(client_id=client_id, db_path=db_path)
    known_states = _known_state_codes(db_path=db_path)
    single_reader = not (extraction.get("provider_b") or extraction.get("model_b"))

    scored: list[tuple[FieldFacts, FieldScore]] = []
    for row in field_rows:
        field_key = str(row["field_key"])
        facts = FieldFacts(
            field_id=int(row["field_id"]),
            field_key=field_key,
            status=str(row.get("status") or ""),
            reads_agree=row.get("reads_agree"),
            text_layer_match=row.get("text_layer_match"),
            is_handwritten=bool(row.get("is_handwritten")),
            in_region=bool(row.get("in_region")),
            value=(row.get("value_a") or row.get("value_b") or None),
            value_a=row.get("value_a"), value_b=row.get("value_b"),
            single_reader=single_reader or (
                (row.get("value_a") not in (None, "")) and row.get("value_b") in (None, "")
            ),
            involved_checks=_involved_checks(field_key, check_rows),
            check_count=len(check_rows),
            required=levels.get(field_key) == LEVEL_REQUIRED,
        )
        scored.append((facts, score_field(facts, config, known_state_codes=known_states)))

    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        try:
            for facts, result in scored:
                conn.execute(
                    "DELETE FROM invoice_doc_field_scores WHERE field_id = ?", (facts.field_id,),
                )
                conn.execute(
                    "INSERT INTO invoice_doc_field_scores (field_id, score, signals_json, caps_json, "
                    "formula_version, threshold_used, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        facts.field_id, result.score,
                        json.dumps([s.as_json() for s in result.signals]),
                        json.dumps([c.as_json() for c in result.caps]),
                        result.formula_version, result.threshold_used, now,
                    ),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "extraction_id": extraction_id,
        "document_id": document_id,
        "scored": sum(1 for _f, r in scored if r.score is not None),
        "no_score": sum(1 for _f, r in scored if r.score is None),
        "flagged": sum(1 for _f, r in scored if r.flagged and r.score is not None),
        "threshold": config.threshold,
        "formula_version": config.formula_version,
    }


def _known_state_codes(*, db_path=None) -> Optional[set[str]]:
    try:
        from src.rules import service as rules_service

        return {row["code"] for row in rules_service.list_state_codes(db_path=db_path)}
    except Exception:  # noqa: BLE001 — a missing table must not stop a score
        return None


# ---------------------------------------------------------------------------
# Corrections: the human record
# ---------------------------------------------------------------------------


def record_correction(
    field_id: int, action: str, *, actor: str, value_after: Optional[str] = None,
    reason_code: Optional[str] = None, reason_text: Optional[str] = None, db_path=None,
) -> dict[str, Any]:
    """Log one human decision about one field — INCLUDING "confirmed as is".

    The confirmation is the point: a log that only records edits can never show
    that a score was RIGHT, and a score that is never shown to be right cannot be
    calibrated.

    Rejected for an upload that has been exported: once a document is filed, its
    history must not move. The value itself is never modified here either — this
    writes the correction log, and the caller's own edit path owns the value.
    """
    if action not in ACTIONS:
        raise InvoiceExtractError(
            f"'{action}' is not a correction action ({', '.join(ACTIONS)})."
        )
    if reason_code is not None and reason_code not in REASON_CODES:
        raise InvoiceExtractError(
            f"'{reason_code}' is not a reason code ({', '.join(REASON_CODES)})."
        )
    if action == ACTION_EDITED and (value_after is None or str(value_after).strip() == ""):
        raise InvoiceExtractError("An edit must record the value it was changed to.")

    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT f.field_id, f.field_key, f.value_a, f.value_b, f.is_handwritten, "
            "f.extraction_id, f.document_id, d.upload_id, d.category, u.client_id, u.status AS upload_status, "
            "e.provider_a, e.model_a, e.provider_b, e.model_b, "
            "s.score, s.formula_version, s.threshold_used, "
            "a.level AS applicability_level "
            "FROM invoice_doc_fields f "
            "JOIN invoice_documents d ON d.document_id = f.document_id "
            "LEFT JOIN invoice_uploads u ON u.upload_id = d.upload_id "
            "LEFT JOIN invoice_doc_extractions e ON e.extraction_id = f.extraction_id "
            "LEFT JOIN invoice_doc_field_scores s ON s.field_id = f.field_id "
            "LEFT JOIN invoice_doc_applicability a ON a.document_id = f.document_id "
            "AND a.field_key = f.field_key "
            "WHERE f.field_id = ?",
            (field_id,),
        )
        row = _dict_row(cur, cur.fetchone())
        if row is None:
            raise InvoiceExtractError(f"Unknown field {field_id}.")

        upload_status = str(row.get("upload_status") or "")
        # ONE definition of "this upload must not change any more" — the legacy
        # service and the review screen consult the same rule (immutable.py).
        if is_immutable_status(upload_status):
            raise InvoiceExtractError(
                f"This upload is {upload_status} — it has been filed, so its history is "
                "immutable and a correction cannot be recorded against it."
            )

        score_at_time = row.get("score")
        threshold = row.get("threshold_used")
        was_flagged = bool(
            score_at_time is None or (threshold is not None and float(score_at_time) < float(threshold))
        )
        before = row.get("value_a") or row.get("value_b")
        extraction_models = json.dumps({
            "read_a": {"provider": row.get("provider_a"), "model": row.get("model_a")},
            "read_b": {"provider": row.get("provider_b"), "model": row.get("model_b")},
        })
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO invoice_field_corrections (field_id, document_id, client_id, field_key, "
            "value_before, value_after, action, reason_code, reason_text, actor, at, score_at_time, "
            "was_flagged, formula_version, document_category, extraction_models, is_handwritten) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                field_id, int(row["document_id"]), row.get("client_id"), str(row["field_key"]),
                before, value_after, action, reason_code, reason_text, actor, now,
                score_at_time, 1 if was_flagged else 0, row.get("formula_version"),
                row.get("category"), extraction_models, 1 if row.get("is_handwritten") else 0,
            ),
        )
        correction_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
    finally:
        conn.close()

    # A new correction UN-SIGNS any standing review: the reviewer signed off a
    # document whose value has now changed. Recorded in the document log.
    from src.invoice_extract import doc_actions

    doc_actions.clear_reviewed(
        int(row["document_id"]), actor=actor, triggered_by="field_correction",
        reason=f"correction on {row['field_key']} ({action})",
        field_key=str(row["field_key"]), db_path=db_path,
    )

    return {
        "correction_id": correction_id,
        "field_id": field_id,
        "action": action,
        "value_before": before,
        "value_after": value_after,
        "score_at_time": score_at_time,
        "was_flagged": was_flagged,
    }


def list_corrections(
    *, client_id: Optional[int] = None, document_id: Optional[int] = None, db_path=None,
) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        sql = "SELECT * FROM invoice_field_corrections WHERE 1 = 1"
        params: list[Any] = []
        if client_id is not None:
            sql += " AND client_id = ?"
            params.append(client_id)
        if document_id is not None:
            sql += " AND document_id = ?"
            params.append(document_id)
        sql += " ORDER BY correction_id"
        cur = conn.execute(sql, tuple(params))
        return [_dict_row(cur, row) for row in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reading scores back
# ---------------------------------------------------------------------------


def get_scores(document_id: int, *, db_path=None) -> dict[str, dict[str, Any]]:
    """field_key -> score row (with parsed signals and caps) for a document."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT f.field_key, s.score, s.signals_json, s.caps_json, s.formula_version, "
            "s.threshold_used, s.computed_at, f.field_id, f.status "
            "FROM invoice_doc_fields f "
            "JOIN invoice_doc_extractions e ON e.extraction_id = f.extraction_id "
            "LEFT JOIN invoice_doc_field_scores s ON s.field_id = f.field_id "
            "WHERE f.document_id = ? AND e.is_active = 1",
            (document_id,),
        )
        out: dict[str, dict[str, Any]] = {}
        for row in cur.fetchall():
            record = _dict_row(cur, row)
            assert record is not None
            record["signals"] = json.loads(record.get("signals_json") or "[]")
            record["caps"] = json.loads(record.get("caps_json") or "[]")
            out[str(record["field_key"])] = record
        return out
    finally:
        conn.close()


def field_confidence(document_id: int, field_key: str, *, db_path=None) -> Optional[float]:
    row = get_scores(document_id, db_path=db_path).get(field_key)
    return None if row is None else row.get("score")
