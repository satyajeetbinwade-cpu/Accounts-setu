"""Post-matching AI analysis layer.

Thin and procedural, consistent with the rest of this PoC. OpenRouter is the
only provider — no class hierarchy, no provider abstraction. This module is
the single entry point the UI calls (on-demand only): it performs routing,
caching (reusing the review carry-forward fingerprint), the API call,
validation, and persistence into the immutable `ai_analysis` table.

Cost/constraint driven: no call may happen as a side effect of a run; every
call is triggered by an explicit click, and previously-analyzed items are
served from cache via their fingerprint (which is stable across re-runs but
changes when the underlying record changes).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from src import db

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "ai_config.yaml"

REQUIRED_RESPONSE_KEYS = [
    "issue_summary",
    "probable_causes",
    "suggested_fix",
    "reasoning",
    "confidence",
    "data_sufficient",
]
ALLOWED_CONFIDENCE = ("high", "medium", "low")

# Books/portal fields chosen per record shape. Only present (non-null) values
# are sent — null fields are omitted rather than sent as empty placeholders.
GST_BOOKS_FIELDS = [
    ("invoice_number", "invoice/voucher no."),
    ("invoice_date", "date"),
    ("party_name", "party name"),
    ("gstin", "GSTIN"),
    ("taxable_value", "taxable value"),
    ("cgst", "CGST"),
    ("sgst", "SGST"),
    ("igst", "IGST"),
    ("cess", "Cess"),
    ("total_tax", "total tax"),
    ("invoice_value", "invoice value"),
]
GST_PORTAL_FIELDS = GST_BOOKS_FIELDS

TDS_BOOKS_FIELDS = [
    ("voucher_number", "voucher no."),
    ("challan_number", "challan no."),
    ("deposit_date", "date"),
    ("deductee_name", "party name"),
    ("pan", "PAN"),
    ("section", "section code"),
    ("amount_paid_credited", "amount paid/credited"),
    ("tax_deducted", "tax deducted"),
    ("tax_deposited", "tax deposited"),
]
TDS_PORTAL_FIELDS = TDS_BOOKS_FIELDS


class AIAnalysisError(RuntimeError):
    """Base error for the AI analysis layer. Raised on config/routing/parse
    failures; the layer fails loudly rather than guessing."""


def load_ai_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load and minimally validate the AI analysis config YAML."""
    cfg_path = Path(path) if path else CONFIG_PATH
    if not cfg_path.exists():
        raise AIAnalysisError(f"AI config file not found: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise AIAnalysisError("AI config root must be a mapping")
    for section in ("openrouter", "models", "routing", "scope", "limits"):
        if section not in raw:
            raise AIAnalysisError(f"AI config is missing required key: {section}")
    orc = raw["openrouter"]
    if not orc.get("api_key_env"):
        raise AIAnalysisError("openrouter.api_key_env is required")
    if not raw["models"].get("complex") or not raw["models"].get("simple"):
        raise AIAnalysisError("models.complex and models.simple are required")
    scope = raw["scope"]
    if not isinstance(scope.get("analyzable_classifications"), list):
        raise AIAnalysisError("scope.analyzable_classifications must be a list")
    if "Matched" in scope["analyzable_classifications"]:
        raise AIAnalysisError("'Matched' must never be an analyzable classification")
    return raw


def is_analyzable_classification(classification: str, config: dict[str, Any]) -> bool:
    """True if the classification is eligible for AI analysis."""
    return classification in config["scope"]["analyzable_classifications"]


def _registry_model() -> Optional[str]:
    """The reconciliation-explanation touchpoint's model from the central AI
    model registry (Setup → AI Models).

    The registry is the single place every AI touchpoint's model is set.
    This layer reads it first so a change made there takes effect without
    editing config/ai_config.yaml. Best-effort: any registry failure falls
    through to the YAML routing, so a registry problem can never break
    analysis.
    """
    try:
        from src.ai_models import service as ai_models

        return ai_models.effective_model("recon_explanation")
    except Exception:  # noqa: BLE001
        return None


def select_model(
    classification: str, difference_type: Optional[str], config: dict[str, Any]
) -> tuple[str, str]:
    """Return (model_id, routing_reason).

    Routing: difference_type override first, then classification, then default.
    The routing_reason records *why* a model was chosen so routing behaviour
    is auditable without re-deriving it from config.

    The central AI model registry (Setup → AI Models) takes precedence over
    the YAML routing when it has a model for this touchpoint — the registry
    is the single place models are meant to be changed from.
    """
    registry_model = _registry_model()
    if registry_model:
        return registry_model, "AI model registry (Setup → AI Models)"

    routing = config["routing"]
    by_dt = routing.get("by_difference_type", {})
    if difference_type and difference_type in by_dt:
        route = by_dt[difference_type]
        return config["models"][route], f"difference_type={difference_type} → {route}"

    by_cls = routing.get("by_classification", {})
    if classification in by_cls:
        route = by_cls[classification]
        return config["models"][route], f"classification={classification} → {route}"

    route = routing.get("default", "simple")
    return config["models"][route], f"default={route}"


def _record_to_lines(record: Any, recon_type: str, fields) -> list[str]:
    """Render the non-null fields of a record as labelled lines."""
    if not isinstance(record, dict):
        return []
    lines = []
    for key, label in fields:
        v = record.get(key)
        if v is None or v == "":
            continue
        if isinstance(v, float):
            lines.append(f"- {label}: {v:,.2f}")
        else:
            lines.append(f"- {label}: {v}")
    return lines


def _c5_runtime_context(recon_type: str) -> str:
    """C5 retrofit: retrieve the live instruction-library context for the
    touchpoint matching this recon type. Returns '' on any error (C5 is a
    non-blocking input — its absence must never break analysis)."""
    try:
        from src.c5 import service as c5

        touchpoint = "tds_classification" if recon_type == "TDS" else "gst_classification"
        return c5.runtime_context(touchpoint)
    except Exception:  # noqa: BLE001
        return ""


def _percent_gap(books: dict, portal: dict) -> str:
    """Helper: express the numeric gap between two amount fields, if both
    are present, as a percentage for the model. Returns '' if not computable."""
    for key in ("invoice_value", "tax_deducted", "amount_paid_credited", "taxable_value"):
        b = books.get(key) if isinstance(books, dict) else None
        p = portal.get(key) if isinstance(portal, dict) else None
        if b is None or p is None:
            continue
        try:
            bf, pf = float(b), float(p)
        except (TypeError, ValueError):
            continue
        if pf == 0:
            continue
        return f"- percentage gap on {key}: {(bf - pf) / abs(pf) * 100:+.2f}%"
    return ""


def build_prompt(
    result_row: dict[str, Any],
    books_record: Any,
    portal_record: Any,
    config: dict[str, Any],
    *,
    recon_type: str,
    period: str,
    matching_config: Optional[dict[str, Any]] = None,
    c5_context: str = "",
) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for a single item.

    Sends only what pertains to this item — never the full config, the whole
    result set, other clients' data, or records not part of this item's match.
    Null fields are omitted; the absence of a portal record is stated with
    the *reason* (no counterpart found vs no counterpart expected).

    `config` is the AI config (routing/models). `matching_config` is the
    matching-rules config (tolerances/rates/thresholds) that actually produced
    the flag — taken from the run's config_snapshot where available.
    """
    system_prompt = (
        "You are assisting a chartered accountancy firm in India with GST and TDS "
        "reconciliation review. You are given a single reconciliation discrepancy "
        "that a rule-based matching engine has already classified. Your job is to "
        "explain the likely underlying cause and suggest what the accountant should "
        "do next.\n\n"
        "You are NOT re-classifying the item and NOT recomputing the match. Treat the "
        "engine's classification as given.\n\n"
        "Rules:\n"
        "- Use Indian accounting and tax terminology (ITC, GSTR-2B, IMS, TDS section "
        "codes, Form 26AS, challan, deductor/deductee).\n"
        "- Ground every statement in the specific figures provided. Do not invent "
        "invoice numbers, amounts, dates, GSTINs, PANs or section codes that are "
        "not in the input.\n"
        "- If the available data is insufficient to determine a cause, say so "
        "explicitly rather than speculating.\n"
        "- Never recommend an automatic or unreviewed posting to the books. Every "
        "suggested fix must be something a person reviews and approves.\n"
        "- Respond with a single valid JSON object and nothing else. No preamble, "
        "no explanation outside the JSON, no markdown code fences."
    )

    classification = result_row.get("classification", "")
    difference_type = result_row.get("difference_type")

    lines: list[str] = []
    lines.append(
        f"Reconciliation type: {'GST 2A' if recon_type == 'GST' else 'TDS 2B'}"
    )
    lines.append(f"Period: {period}")
    lines.append(f"Classification: {classification}")
    if difference_type:
        lines.append(f"Difference type: {difference_type}")

    lines.append("\nBooks-side record:")
    b_fields = GST_BOOKS_FIELDS if recon_type == "GST" else TDS_BOOKS_FIELDS
    books_lines = _record_to_lines(books_record, recon_type, b_fields)
    lines.extend(books_lines if books_lines else ["- (no books record on this side)"])

    lines.append("\nPortal-side record:")
    p_fields = GST_PORTAL_FIELDS if recon_type == "GST" else TDS_PORTAL_FIELDS
    portal_lines = _record_to_lines(portal_record, recon_type, p_fields)
    if portal_lines:
        lines.extend(portal_lines)
    elif difference_type and not portal_record:
        # Books-internal validation finding — no portal counterpart was expected.
        lines.append(
            "No portal record — this is a books-internal validation finding "
            "(rate/threshold check), not a portal comparison"
        )
    else:
        # No portal counterpart found.
        lines.append(
            f'No portal record — this item was classified "{classification}"'
        )

    match_reason = result_row.get("match_reason")
    if match_reason:
        lines.append(f'\nEngine match reason: "{match_reason}"')

    if matching_config:
        mc = matching_config.get(recon_type.lower(), matching_config)
        if isinstance(mc, dict):
            lines.append("\nConfig values that produced the flag:")
            if "amount_tolerance" in mc:
                lines.append(f"- amount tolerance: {mc['amount_tolerance']}")
            if "rounding_tolerance" in mc:
                lines.append(f"- rounding tolerance: {mc['rounding_tolerance']}")
            if "date_tolerance_days" in mc:
                lines.append(f"- date tolerance days: {mc['date_tolerance_days']}")
            if recon_type == "TDS":
                section = (books_record or {}).get("section")
                section_table = mc.get("section_table", {})
                if section and isinstance(section_table, dict) and section in section_table:
                    lines.append(
                        f"- expected section {section} rates: {section_table[section].get('rates')}"
                    )
                    lines.append(
                        f"- single transaction threshold: {section_table[section].get('single_transaction_threshold')}"
                    )
                    lines.append(
                        f"- annual aggregate threshold: {section_table[section].get('annual_aggregate_threshold')}"
                    )
            else:
                if "valid_rate_slabs" in mc:
                    lines.append(f"- valid rate slabs: {mc['valid_rate_slabs']}")

    if isinstance(books_record, dict) and isinstance(portal_record, dict):
        gap = _percent_gap(books_record, portal_record)
        if gap:
            lines.append("\nComputed gap:")
            lines.append(gap)

    matched_ids = result_row.get("matched_record_ids")
    if matched_ids:
        try:
            ids = matched_ids if isinstance(matched_ids, list) else json.loads(matched_ids)
        except (TypeError, ValueError):
            ids = []
        if ids:
            lines.append(f"\nAggregated match constituent record count: {len(ids)}")

    # C5 retrofit: append the live instruction-library context (firm-wide +
    # client-scoped instructions for this touchpoint) so the AI reads it
    # alongside its core prompt. Only appended when non-empty.
    if c5_context:
        lines.append("\n" + c5_context)

    user_prompt = "\n".join(lines)
    return system_prompt, user_prompt


def _get_api_key(config: dict[str, Any]) -> str:
    """Read the API key from the named env var. Never logged, stored, or
    returned into any persistent context by callers."""
    env_name = config["openrouter"]["api_key_env"]
    key = os.environ.get(env_name, "")
    if not key:
        raise AIAnalysisError(
            f"config_error|Environment variable {env_name!r} is not set"
        )
    return key


def call_openrouter(
    system_prompt: str,
    user_prompt: str,
    model_id: str,
    config: dict[str, Any],
) -> tuple[str, int]:
    """Call OpenRouter. Returns (raw_text, latency_ms).

    Raises AIAnalysisError with an error "type" embedded in .args[0] so the
    caller can classify it by error_type. We do not use a third-party HTTP
    client (requests is not a dependency of this PoC); urllib keeps the stack
    locked.
    """
    api_key = _get_api_key(config)
    orc = config["openrouter"]
    base_url = orc["base_url"]
    timeout = int(orc.get("timeout_seconds", 120))

    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": int(orc.get("max_tokens", 1500)),
        "temperature": float(orc.get("temperature", 0.1)),
    }
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        base_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        latency = int((time.monotonic() - start) * 1000)
        code = e.code
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if code in (401, 403):
            raise AIAnalysisError(f"auth_error|HTTP {code} {body}")
        if code == 429:
            raise AIAnalysisError(f"rate_limit|HTTP {code} {body}")
        # 5xx or other transport failure
        raise AIAnalysisError(f"api_error|HTTP {code} {body}")
    except (TimeoutError, socket.timeout) as e:
        latency = int((time.monotonic() - start) * 1000)
        raise AIAnalysisError(f"timeout|request exceeded timeout: {e}")
    except (urllib.error.URLError, OSError) as e:
        latency = int((time.monotonic() - start) * 1000)
        raise AIAnalysisError(f"api_error|{e.__class__.__name__}: {e}")

    latency = int((time.monotonic() - start) * 1000)
    return raw, latency


def _extract_json_object(text: str) -> str:
    """Strip fences and extract the outermost balanced {...} block."""
    if not text:
        raise AIAnalysisError("parse_error|empty response")
    s = text.strip()
    # Strip ```json ... ``` fences
    if s.startswith("```"):
        s = s.strip("`")
        # remove a leading language tag like "json"
        first_newline = s.find("\n")
        if first_newline != -1:
            head = s[:first_newline].strip().lower()
            if head in ("json", "javascript", ""):
                s = s[first_newline + 1 :]
    start = s.find("{")
    if start == -1:
        raise AIAnalysisError("parse_error|no JSON object found in response")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    raise AIAnalysisError("parse_error|unbalanced JSON object in response")


def parse_and_validate(raw_text: str) -> dict[str, Any]:
    """Parse the model's JSON response and validate against the required
    schema. Raises AIAnalysisError on parse/schema failure. No coercion,
    patching, or filling of missing fields."""
    obj_str = _extract_json_object(raw_text)
    try:
        obj = json.loads(obj_str)
    except (TypeError, ValueError) as e:
        raise AIAnalysisError(f"parse_error|{e}") from e

    if not isinstance(obj, dict):
        raise AIAnalysisError("schema_error|parsed JSON is not an object")

    for key in REQUIRED_RESPONSE_KEYS:
        if key not in obj:
            raise AIAnalysisError(f"schema_error|missing key {key!r}")

    causes = obj.get("probable_causes")
    if not isinstance(causes, list) or not causes:
        raise AIAnalysisError("schema_error|probable_causes must be a non-empty list")

    confidence = obj.get("confidence")
    if confidence not in ALLOWED_CONFIDENCE:
        raise AIAnalysisError("schema_error|confidence must be high|medium|low")

    reasoning = obj.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise AIAnalysisError("schema_error|reasoning is empty")

    data_sufficient = obj.get("data_sufficient")
    if not isinstance(data_sufficient, bool):
        raise AIAnalysisError("schema_error|data_sufficient must be a boolean")

    return obj


def get_cached_analysis(
    conn: sqlite3.Connection, fingerprint: str
) -> Optional[dict[str, Any]]:
    """Most recent successful analysis row for this fingerprint, or None."""
    row = conn.execute(
        """
        SELECT id, run_id, result_id, fingerprint, recon_type, classification,
               difference_type, model_used, routing_reason, status,
               issue_summary, probable_causes, suggested_fix, reasoning,
               confidence, data_sufficient, raw_response, error_type,
               error_detail, latency_ms, created_at
        FROM ai_analysis
        WHERE fingerprint = ? AND status = 'success'
        ORDER BY id DESC
        LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()
    if row is None:
        return None
    cols = [
        "id", "run_id", "result_id", "fingerprint", "recon_type", "classification",
        "difference_type", "model_used", "routing_reason", "status", "issue_summary",
        "probable_causes", "suggested_fix", "reasoning", "confidence",
        "data_sufficient", "raw_response", "error_type", "error_detail",
        "latency_ms", "created_at",
    ]
    d = dict(zip(cols, row))
    if d.get("probable_causes"):
        try:
            d["probable_causes"] = json.loads(d["probable_causes"])
        except (TypeError, ValueError):
            pass
    return d


def _insert_analysis(conn: sqlite3.Connection, rec: dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO ai_analysis
            (run_id, result_id, fingerprint, recon_type, classification,
             difference_type, model_used, routing_reason, status,
             issue_summary, probable_causes, suggested_fix, reasoning,
             confidence, data_sufficient, raw_response, error_type,
             error_detail, latency_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rec["run_id"],
            rec["result_id"],
            rec["fingerprint"],
            rec["recon_type"],
            rec["classification"],
            rec.get("difference_type"),
            rec["model_used"],
            rec["routing_reason"],
            rec["status"],
            rec.get("issue_summary"),
            rec.get("probable_causes"),
            rec.get("suggested_fix"),
            rec.get("reasoning"),
            rec.get("confidence"),
            rec.get("data_sufficient"),
            rec.get("raw_response"),
            rec.get("error_type"),
            rec.get("error_detail"),
            rec.get("latency_ms"),
            rec["created_at"],
        ),
    )
    return cur.lastrowid


def _result_context(
    conn: sqlite3.Connection, result_id: int
) -> dict[str, Any]:
    """Load the match_results row plus its run's recon_type/period."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM match_results WHERE result_id = ?", (result_id,)
    ).fetchone()
    if row is None:
        raise AIAnalysisError(f"config_error|no match_results row for result_id {result_id}")
    result = dict(row)
    run = conn.execute(
        "SELECT recon_type, period, config_snapshot FROM runs WHERE run_id = ?",
        (result["run_id"],),
    ).fetchone()
    if run is None:
        raise AIAnalysisError("config_error|run not found")
    result["_recon_type"] = run["recon_type"]
    result["_period"] = run["period"]
    result["_matching_config"] = _parse_config_snapshot(run["config_snapshot"])
    for field in ("books_record", "portal_record", "matched_record_ids"):
        if result.get(field):
            try:
                result[field] = json.loads(result[field])
            except (TypeError, ValueError):
                pass
    return result


def _parse_config_snapshot(snapshot: Optional[str]) -> Optional[dict[str, Any]]:
    """Parse a run's config_snapshot (YAML text) into a dict, or None."""
    if not snapshot:
        return None
    try:
        return yaml.safe_load(snapshot)
    except yaml.YAMLError:
        return None


def analyze_result(
    conn: sqlite3.Connection,
    result_id: int,
    config: dict[str, Any],
    force: bool = False,
) -> dict[str, Any]:
    """Analyze a single result. The single entry point the UI calls.

    Performs cache check (unless force), eligibility check, routing, API call,
    parse/validate, and persistence. Returns a dict rendered directly by the
    UI for both success and failure cases.

    Raises AIAnalysisError only for ineligible items (e.g. Matched); a failed
    API call or validation is persisted as status='failed' and returned, not
    raised.
    """
    result = _result_context(conn, result_id)
    classification = result["classification"]
    recon_type = result["_recon_type"]
    period = result["_period"]

    if not is_analyzable_classification(classification, config):
        raise AIAnalysisError(
            f"config_error|classification {classification!r} is not eligible for AI analysis "
            "(Matched is never analyzable)"
        )

    fingerprint = result.get("fingerprint")

    if not force:
        cached = get_cached_analysis(conn, fingerprint) if fingerprint else None
        if cached is not None:
            cached["_cached"] = True
            return cached

    model_id, routing_reason = select_model(
        classification, result.get("difference_type"), config
    )

    base_rec = {
        "run_id": str(result["run_id"]),
        "result_id": result_id,
        "fingerprint": fingerprint,
        "recon_type": recon_type,
        "classification": classification,
        "difference_type": result.get("difference_type"),
        "model_used": model_id,
        "routing_reason": routing_reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # C5 retrofit: pull the live instruction-library context for this
    # touchpoint (TDS -> tds_classification; GST -> gst_classification) so
    # the AI reads firm guidance alongside its core prompt. Best-effort — a
    # C5 failure must never break analysis (empty context on any error).
    c5_context = _c5_runtime_context(recon_type)
    if c5_context:
        base_rec["c5_context_used"] = True

    system_prompt, user_prompt = build_prompt(
        result,
        result.get("books_record"),
        result.get("portal_record"),
        config,
        recon_type=recon_type,
        period=period,
        matching_config=result.get("_matching_config"),
        c5_context=c5_context,
    )

    try:
        raw_text, latency_ms = call_openrouter(
            system_prompt, user_prompt, model_id, config
        )
    except AIAnalysisError as e:
        base_rec.update(_failure_from_exc(e))
        base_rec["latency_ms"] = None
        rec_id = _insert_analysis(conn, base_rec)
        conn.commit()
        return _as_response(base_rec, rec_id)

    # Parse/validate — failures are stored, not raised.
    try:
        parsed = parse_and_validate(raw_text)
    except AIAnalysisError as e:
        base_rec.update(_failure_from_exc(e))
        base_rec["raw_response"] = raw_text
        base_rec["latency_ms"] = latency_ms
        rec_id = _insert_analysis(conn, base_rec)
        conn.commit()
        return _as_response(base_rec, rec_id)

    base_rec.update(
        {
            "status": "success",
            "issue_summary": parsed["issue_summary"],
            "probable_causes": json.dumps(parsed["probable_causes"]),
            "suggested_fix": parsed["suggested_fix"],
            "reasoning": parsed["reasoning"],
            "confidence": parsed["confidence"],
            "data_sufficient": 1 if parsed["data_sufficient"] else 0,
            "raw_response": raw_text,
            "latency_ms": latency_ms,
        }
    )
    rec_id = _insert_analysis(conn, base_rec)
    conn.commit()

    out = dict(base_rec)
    out["probable_causes"] = parsed["probable_causes"]
    out["id"] = rec_id
    out["_cached"] = False
    return out


def _failure_from_exc(exc: AIAnalysisError) -> dict[str, Any]:
    """Split 'type|detail' error message into error_type / error_detail."""
    msg = str(exc)
    if "|" in msg:
        err_type, detail = msg.split("|", 1)
    else:
        err_type, detail = "api_error", msg
    # Only an allowed error type should be stored — collapse unknown types.
    allowed = {
        "auth_error", "rate_limit", "timeout", "api_error",
        "parse_error", "schema_error", "config_error",
    }
    if err_type not in allowed:
        detail = msg
        err_type = "api_error"
    return {"status": "failed", "error_type": err_type, "error_detail": detail}


def _as_response(rec: dict[str, Any], rec_id: int) -> dict[str, Any]:
    out = dict(rec)
    out["id"] = rec_id
    if isinstance(out.get("probable_causes"), str):
        try:
            out["probable_causes"] = json.loads(out["probable_causes"])
        except (TypeError, ValueError):
            pass
    return out


def analyze_group(
    conn: sqlite3.Connection,
    result_ids: list[int],
    config: dict[str, Any],
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Analyze result_ids sequentially. A failure on one item does not abort
    the rest — it is recorded as failed. Returns per-item outcomes, a cached
    count, and a fresh (new API call) count."""
    outcomes: list[dict[str, Any]] = []
    cached = 0
    fresh = 0
    total = len(result_ids)
    for i, rid in enumerate(result_ids, start=1):
        if progress_cb is not None:
            progress_cb(i, total)
        out = analyze_result(conn, rid, config, force=False)
        out["_is_cached"] = bool(out.get("_cached"))
        if out["_is_cached"]:
            cached += 1
        else:
            fresh += 1
        outcomes.append(out)
    return {"outcomes": outcomes, "cached": cached, "fresh": fresh, "total": total}


def count_cached_and_new(
    conn: sqlite3.Connection, result_ids: list[int], config: dict[str, Any]
) -> tuple[int, int]:
    """For a group trigger: (cached_count, new_api_call_count), counting only
    eligible items. Used to display the split before firing."""
    cached = 0
    new = 0
    for rid in result_ids:
        try:
            result = _result_context(conn, rid)
        except AIAnalysisError:
            continue
        if not is_analyzable_classification(result["classification"], config):
            continue
        fp = result.get("fingerprint")
        if fp and get_cached_analysis(conn, fp) is not None:
            cached += 1
        else:
            new += 1
    return cached, new


def get_latest_analysis(
    conn: sqlite3.Connection, result_id: int
) -> Optional[dict[str, Any]]:
    """Most recent analysis row (any status) for a result_id — used to render
    the already-analyzed / failed states in the UI on load (no call)."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT id, run_id, result_id, fingerprint, recon_type, classification,
               difference_type, model_used, routing_reason, status,
               issue_summary, probable_causes, suggested_fix, reasoning,
               confidence, data_sufficient, raw_response, error_type,
               error_detail, latency_ms, created_at
        FROM ai_analysis
        WHERE result_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (result_id,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("probable_causes"):
        try:
            d["probable_causes"] = json.loads(d["probable_causes"])
        except (TypeError, ValueError):
            pass
    d["_cached"] = False
    return d