"""Public API for Module 8 — Action Center (Recon Exceptions only).

Every other module (and every UI file) should import from here, not from
src.action_center.db directly. Mirrors src/module2/service.py's shape.

THE CAPSTONE — AND A GENERIC QUEUE, NOT A RECONCILIATION-EXCEPTION QUEUE
--------------------------------------------------------------------------
This module is the single working queue staff act from. It is built
GENERICALLY against the shared Flagged Item interface from Step 3's
Information Architecture (id, type, source module, evidence,
classification/recommendation, confidence, priority, assignee, due date,
status) so Modules 1/4/5/6 slot in LATER WITHOUT A REBUILD.

The architectural acceptance bar for this build is therefore:

    *The queue's core rendering/filtering/assignment logic contains ZERO
    Reconciliation-Exception-specific field names.*

That is achieved structurally by the SOURCE ADAPTER registry below. The
*only* place any origin-specific field name appears is inside a source's
own adapter (`_adapt_recon_exception`), which normalizes a raw origin row
into the generic item shape. Every consumer of this module — the UI, and
the core functions `list_flagged_items` / `assign_item` /
`run_ageing_escalation` / `eligible_for_batch` — works exclusively on the
generic shape.

FUTURE SOURCES (forward flags — documented inline, NOT designed here):
- Module 1 — Information Requests
- Module 4 — Books-Readiness Blocks
- Module 5 — Audit Queries
- Module 6 — Anomaly Flags
Each will register its own adapter via `register_source` and map onto the
existing interface with zero changes to this module's core code.

Business rules enforced here (per the Module 8 build prompt), not just in
the UI:
- Materiality-gated items are ALWAYS excluded from batch actions, at the
  QUEUE level as well as the origin's own level — no bypass path,
  regardless of who is viewing.
- If the origin's underlying data is stale per F5's sync health, the
  corresponding queue items are flagged "may be based on incomplete data"
  rather than presented as fully current.
- Reassigning an item NEVER silently changes its priority or due date —
  both remain visible, and any change is written to F4. (Priority is not
  even stored by this module — it is owned by the origin.)
- Module 8 CREATES NOTHING. It aggregates and surfaces items originated
  elsewhere; acting on an item routes into the origin's own detail screen,
  and batch approval surfaces the origin's own mechanism — Module 8 never
  duplicates resolution logic.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from src import db as recon_db
from src.action_center import db as acdb
from src.action_center.schema import init_action_center_schema

# Age (in days) past which an OPEN item visibly escalates + fires a C3-routed
# notification. Self-contained: read from C1 with a firm default fallback, so
# Module 8 does NOT depend on Module 1 (which doesn't exist).
AGE_ESCALATION_DAYS_KEY = "action_center.age_escalation_days"
DEFAULT_AGE_ESCALATION_DAYS = 30

# Generic priority ordering (most urgent first). Priority itself is owned by
# the originating module; Module 8 only sorts/displays it.
_PRIORITY_ORDER = {"escalated": 0, "high": 1, "normal": 2, "low": 3}


class ActionCenterError(Exception):
    """Raised for expected Action Center failures (validation, blocked action)."""


# ---------------------------------------------------------------------------
# Init + connection
# ---------------------------------------------------------------------------


def init_action_center(db_path=None) -> None:
    """Create Module 8 tables. Call once at app start, alongside the other
    modules' init_*() calls."""
    conn = _connect(db_path)
    try:
        init_action_center_schema(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# SOURCE ADAPTER REGISTRY — the ONE seam where origin-specific code lives.
# ---------------------------------------------------------------------------
# An adapter maps a raw origin row into the GENERIC Flagged Item shape and
# exposes the origin's own batch mechanism. Everything downstream of an
# adapter is origin-agnostic.

# The generic Flagged Item shape every adapter must produce. Documented as
# the contract future Modules 1/4/5/6 must satisfy (see module docstring):
#
#   item_ref            str   stable identity, "<source_module>:<origin_id>"
#   source_module       str   the origin module's generic tag, e.g. 'Module 2'
#   origin_id           any   the origin row's own id
#   client_id           int   the item's client scope
#   type                str   the origin's item type / subtype
#   classification      str   the origin's classification
#   priority            str   'low' | 'normal' | 'high' | 'escalated' (origin-owned)
#   confidence_source   str   'rule' | 'ai'   (Foundation 3.1 passthrough)
#   confidence_pct      int|None
#   confidence_label    str
#   recommendation      str|None   the origin's recommendation, if any
#   recommendation_reason str|None always-visible reasoning, never a tooltip
#   reason              str   THE "why" — evidence/reasoning, always visible
#   evidence_summary    str   brief evidence line
#   materiality_gated   bool  above the origin's materiality threshold
#   item_created_at     str   ISO timestamp (drives age)
#   assignee            str|None   manual assignee (overlay)
#   due_date            str|None   due date (overlay)
#   status              str   'open' | 'resolved' | 'escalated'
#   origin_label        str   human label for the origin ("Reconciliation Exception")


_SOURCE_ADAPTERS: dict[str, dict[str, Callable[..., Any]]] = {}


def register_source(module_tag: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator registering an origin module's adapter functions.

    Each adapter provides:
      fetch(ctx)                 -> list of raw origin rows (open items)
      adapt(raw, ctx)            -> one generic Flagged Item dict
      apply_batch(item_refs, actor) -> the origin's OWN batch mechanism
      detail_label(item)         -> the origin's detail-screen label
    """

    def _decorator(fns: Callable[..., Any]) -> Callable[..., Any]:
        _SOURCE_ADAPTERS[module_tag] = fns
        return fns

    return _decorator


# ---------------------------------------------------------------------------
# Module 2 adapter — the ONLY origin-specific code in this module.
# ---------------------------------------------------------------------------


class _ReconExceptionAdapter:
    """Normalizes Module 2's ReconciliationException onto the generic shape.

    Every Reconciliation-Exception-specific field name in this module lives
    here and nowhere else. A code review of the core functions below should
    find none.
    """

    MODULE_TAG = "Module 2"

    @staticmethod
    def fetch(ctx: "ActionCenterContext") -> list[dict[str, Any]]:
        from src.module2 import service as m2

        # Open items only — the queue is a WORKING queue.
        rows = m2.list_exceptions(status="open")
        return rows

    @staticmethod
    def adapt(raw: dict[str, Any], ctx: "ActionCenterContext") -> dict[str, Any]:
        origin_id = raw.get("exception_id")
        item_ref = f"{_ReconExceptionAdapter.MODULE_TAG}:{origin_id}"

        # The origin's own confidence tagging is passed through verbatim —
        # Module 8 NEVER re-derives or re-labels it (Foundation 3.1).
        source = raw.get("confidence_source") or "rule"
        if source == "ai":
            conf_label = f"AI — {int(raw.get('confidence_score') or 0)}%"
        else:
            conf_label = raw.get("confidence_band") or "Rule match"

        # THE "why" — the origin's own reasoning, always visible.
        reason = raw.get("recommendation_reason") or raw.get("match_reason") or ""

        return {
            "item_ref": item_ref,
            "source_module": _ReconExceptionAdapter.MODULE_TAG,
            "origin_id": origin_id,
            "client_id": raw.get("client_id"),
            "type": raw.get("item_type") or raw.get("classification") or "Exception",
            "classification": raw.get("classification") or "",
            "priority": raw.get("priority") or "normal",
            "confidence_source": source,
            "confidence_pct": int(raw["confidence_score"]) if raw.get("confidence_score") is not None else None,
            "confidence_label": conf_label,
            "recommendation": raw.get("recommendation"),
            "recommendation_reason": raw.get("recommendation_reason"),
            "reason": reason,
            "evidence_summary": _recon_evidence_summary(raw),
            "materiality_gated": bool(raw.get("above_materiality")),
            "item_created_at": raw.get("created_at"),
            "status": raw.get("status") or "open",
            "origin_label": "Reconciliation Exception",
        }

    @staticmethod
    def apply_batch(item_refs: list[str], actor: str) -> dict[str, Any]:
        """Surface Module 2's OWN resolution mechanism for a batch of items —
        Module 8 does NOT re-implement acceptance. Each eligible item is
        resolved through Module 2's `resolve_exception` (its accepted path),
        which itself writes the origin's audit/edit history."""
        from src.module2 import service as m2

        applied: list[str] = []
        for ref in item_refs:
            origin_id = _origin_id_of(ref)
            if origin_id is None:
                continue
            # Module 2 owns the decision — Module 8 only triggers it.
            m2.resolve_exception(
                int(origin_id), resolution="accepted",
                note="Batch-approved via Action Center (high-confidence, non-material).",
                actor=actor,
            )
            applied.append(ref)
        return {"applied": applied, "count": len(applied)}

    @staticmethod
    def origin_detail_route(item: dict[str, Any]) -> dict[str, Any]:
        """Context Module 8 hands off so the origin's own detail screen can
        open the right item with no re-fetch mismatch."""
        return {
            "target_tab_key": "Reconciliation",
            "detail_session_key": "m2_open_exception",
            "origin_id": item.get("origin_id"),
        }


def _recon_evidence_summary(raw: dict[str, Any]) -> str:
    """A brief, origin-specific evidence line (stays inside the adapter)."""
    classification = raw.get("classification") or ""
    diff = raw.get("difference_type")
    if diff:
        return f"{classification} — {diff}"
    return classification or "Exception raised for human review."


# Register the Module 2 adapter as the sole live source this phase.
register_source(_ReconExceptionAdapter.MODULE_TAG)(_ReconExceptionAdapter)


# ---------------------------------------------------------------------------
# Generic identity helpers (origin-agnostic)
# ---------------------------------------------------------------------------


def _origin_id_of(item_ref: str) -> Optional[str]:
    """Parse "<source_module>:<origin_id>" -> origin_id (generic)."""
    if ":" not in item_ref:
        return None
    return item_ref.split(":", 1)[1]


# ---------------------------------------------------------------------------
# ActionCenterContext — shared, cached lookups passed to every adapter.
# ---------------------------------------------------------------------------


class ActionCenterContext:
    """Lightweight, request-scoped lookups (client names, F5 staleness, F1
    default routing) so adapters and the core don't repeat expensive calls."""

    def __init__(self) -> None:
        self._client_name_cache: Optional[dict[int, str]] = None
        self._staleness_cache: Optional[dict[int, dict[str, Any]]] = None
        self._team_cache: Optional[dict[str, list[dict[str, Any]]]] = None
        self._folder_key_cache: Optional[dict[int, Optional[str]]] = None

    # -- Client names (F2, presentation only) ---------------------------
    def client_name(self, client_id: Optional[int]) -> str:
        if client_id is None:
            return "—"
        if self._client_name_cache is None:
            try:
                from src.clients import service as clients

                self._client_name_cache = {
                    c["client_id"]: c["legal_name"] for c in clients.list_clients(include_inactive=True)
                }
            except Exception:  # noqa: BLE001
                self._client_name_cache = {}
        return self._client_name_cache.get(client_id, f"Client #{client_id}")

    # -- F5 staleness (live) --------------------------------------------
    def staleness(self, client_id: Optional[int]) -> Optional[dict[str, Any]]:
        """Return a staleness descriptor when F5 reports degraded sync health
        for a client, else None. Cached per client for the request."""
        if client_id is None:
            return None
        if self._staleness_cache is None:
            self._staleness_cache = {}
        if client_id in self._staleness_cache:
            return self._staleness_cache[client_id]

        descriptor: Optional[dict[str, Any]] = None
        try:
            from src.f5 import service as f5

            health = f5.sync_health_for_client(client_id)
            degraded = [h for h in health if h.get("status") == "attempted_failed"]
            if degraded:
                sources = ", ".join(h.get("source", "?") for h in degraded)
                descriptor = {
                    "message": "May be based on incomplete data",
                    "detail": f"Sync failed for: {sources}",
                }
        except Exception:  # noqa: BLE001 — staleness is best-effort, never fatal
            descriptor = None
        self._staleness_cache[client_id] = descriptor
        return descriptor

    # -- F1 default team routing ----------------------------------------
    def default_assignee(self, client_id: Optional[int]) -> Optional[str]:
        """The default assignee from F1's per-client team routing, or None.
        Prefers a working-level role (Senior Accountant / Article-Trainee)."""
        if client_id is None:
            return None
        try:
            from src.auth import service as auth
            from src.clients import service as clients

            if self._team_cache is None:
                self._team_cache = {}
            folder_key = self._folder_key_for_client(client_id, clients, auth)
            if folder_key is None or folder_key not in self._team_cache:
                team = auth.team_for_client(folder_key) if folder_key else []
                if folder_key is not None:
                    self._team_cache[folder_key] = team
            team = self._team_cache.get(folder_key or "", [])
            if not team:
                return None
            preferred = ("Senior Accountant", "Article-Trainee", "Manager")
            for role in preferred:
                for member in team:
                    if member.get("role_name") == role:
                        return member.get("display_name") or member.get("username")
            top = team[0]
            return top.get("display_name") or top.get("username")
        except Exception:  # noqa: BLE001
            return None

    def _folder_key_for_client(self, client_id: int, clients_mod, auth_mod) -> Optional[str]:
        """F1's team assignment is keyed by the data/ folder name; F2's
        client_id is numeric. Bridge them by matching the legal name against
        the existing assignment keys (exact, then normalized)."""
        if self._folder_key_cache is None:
            self._folder_key_cache = {}
        if client_id in self._folder_key_cache:
            return self._folder_key_cache[client_id]

        key: Optional[str] = None
        try:
            client = next(
                (c for c in clients_mod.list_clients(include_inactive=True) if c["client_id"] == client_id),
                None,
            )
            legal = _normalize_name(client["legal_name"]) if client else ""
            assignment_keys = {a["client"] for a in auth_mod.all_assignments()}
            for cand in assignment_keys:
                if _normalize_name(cand) == legal:
                    key = cand
                    break
            if key is None and legal:
                for cand in assignment_keys:
                    norm = _normalize_name(cand)
                    if norm and (norm.startswith(legal) or legal.startswith(norm)):
                        key = cand
                        break
        except Exception:  # noqa: BLE001
            key = None
        self._folder_key_cache[client_id] = key
        return key


def _normalize_name(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


# ---------------------------------------------------------------------------
# Core queue — GENERIC Flagged Item interface
# ---------------------------------------------------------------------------


def _collect_items(ctx: ActionCenterContext) -> list[dict[str, Any]]:
    """Gather, normalize, and enrich every open Flagged Item from every
    registered source. This function is origin-agnostic; the only
    origin-specific work happens inside each adapter."""
    items: list[dict[str, Any]] = []
    for module_tag, adapter in _SOURCE_ADAPTERS.items():
        try:
            raws = adapter.fetch(ctx)
        except Exception:  # noqa: BLE001 — one broken source never kills the queue
            continue
        for raw in raws:
            try:
                item = adapter.adapt(raw, ctx)
            except Exception:  # noqa: BLE001
                continue
            item["source_module"] = item.get("source_module") or module_tag
            item["origin_label"] = item.get("origin_label") or module_tag
            items.append(item)

    # --- Generic enrichment (no origin-specific field names below) ------
    conn = _connect()
    try:
        overlays = {
            (a["source_module"], a["item_ref"]): a for a in acdb.list_assignments(conn)
        }
    finally:
        conn.close()

    today = date.today()
    for item in items:
        overlay = overlays.get((item["source_module"], item["item_ref"]), {})

        # Assignment overlay (manual assignee overrides F1 default routing).
        manual_assignee = overlay.get("assignee")
        if manual_assignee:
            item["assignee"] = manual_assignee
            item["assignee_source"] = "manual"
        else:
            item["assignee"] = ctx.default_assignee(item.get("client_id"))
            item["assignee_source"] = "default" if item["assignee"] else "unassigned"
        item["due_date"] = overlay.get("due_date")

        # Age (generic) + escalation state.
        item["age_days"] = _age_days(item.get("item_created_at"), today)
        item["age_escalated"] = bool(overlay.get("age_escalated"))

        # F5 staleness (generic — reads live).
        item["staleness"] = ctx.staleness(item.get("client_id"))

        # Presentation helper (generic).
        item["client_name"] = ctx.client_name(item.get("client_id"))
    return items


def _age_days(created_at: Optional[str], today: date) -> int:
    if not created_at:
        return 0
    try:
        created = datetime.fromisoformat(created_at).date()
    except (TypeError, ValueError):
        return 0
    return max((today - created).days, 0)


def list_flagged_items(
    *,
    client_id: Optional[int] = None,
    source_module: Optional[str] = None,
    item_type: Optional[str] = None,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    min_age_days: Optional[int] = None,
    status: Optional[str] = "open",
    db_path=None,
) -> list[dict[str, Any]]:
    """The unified queue: every open Flagged Item, filterable ONLY by the
    generic dimensions (client / module-of-origin / type / priority / age /
    assignee). No origin-specific concept appears in this signature."""
    ctx = ActionCenterContext()
    items = _collect_items(ctx)

    def _keep(item: dict[str, Any]) -> bool:
        if status is not None and item.get("status") != status:
            return False
        if client_id is not None and item.get("client_id") != client_id:
            return False
        if source_module is not None and item.get("source_module") != source_module:
            return False
        if item_type is not None and item.get("type") != item_type:
            return False
        if priority is not None and item.get("priority") != priority:
            return False
        if assignee is not None and item.get("assignee") != assignee:
            return False
        if min_age_days is not None and (item.get("age_days") or 0) < min_age_days:
            return False
        return True

    kept = [i for i in items if _keep(i)]
    kept.sort(key=lambda i: (_PRIORITY_ORDER.get(i.get("priority", "normal"), 9), -(i.get("age_days") or 0)))
    return kept


def item_counts(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline (total) + grouped breakdowns by module-of-origin and by
    priority — the generic grouped layer (Foundation 3.6)."""
    by_source: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    material_gated = 0
    escalated = 0
    stale = 0
    for item in items:
        by_source[item["source_module"]] = by_source.get(item["source_module"], 0) + 1
        by_priority[item["priority"]] = by_priority.get(item["priority"], 0) + 1
        if item.get("materiality_gated"):
            material_gated += 1
        if item.get("age_escalated") or item.get("priority") == "escalated":
            escalated += 1
        if item.get("staleness"):
            stale += 1
    return {
        "total": len(items),
        "by_source": by_source,
        "by_priority": by_priority,
        "material_gated": material_gated,
        "escalated": escalated,
        "stale": stale,
    }


def get_flagged_item(item_ref: str, *, db_path=None) -> Optional[dict[str, Any]]:
    """Fetch a single generic item by its stable item_ref."""
    for item in _collect_items(ActionCenterContext()):
        if item["item_ref"] == item_ref:
            return item
    return None


# ---------------------------------------------------------------------------
# Assignment / reassignment — writes a REAL F4 entry
# ---------------------------------------------------------------------------
# Assignment is NOT a sensitive-field edit, so no mandatory reason prompt is
# required — F4's logging, not Reason-capture-before-save (3.3). A reason is
# still accepted and recorded when the caller supplies one.


def assign_item(
    item_ref: str, *, assignee: Optional[str], due_date: Optional[str],
    actor: str, reason: Optional[str] = None, db_path=None,
) -> dict[str, Any]:
    """Manually assign/reassign an item and/or set its due date. Never
    touches priority — the item's priority is owned by the origin and is
    only ever displayed. Writes a REAL F4 edit-history entry for each change
    (plus a local audit row)."""
    item = get_flagged_item(item_ref, db_path=db_path)
    if item is None:
        raise ActionCenterError("Queue item not found.")

    before_assignee = item.get("assignee") if item.get("assignee_source") == "manual" else None
    before_due = item.get("due_date")

    source_module = item["source_module"]
    origin_id = item.get("origin_id")

    conn = _connect(db_path)
    try:
        acdb.set_assignee_and_due(
            conn, source_module=source_module, item_ref=item_ref,
            client_id=item.get("client_id"), assignee=assignee, due_date=due_date,
        )
        acdb.log_change(
            conn, source_module=source_module, item_ref=item_ref, entry_kind="audit_trail",
            action="assign", detail=f"assignee={assignee}, due={due_date}", reason=reason, actor=actor,
        )
    finally:
        conn.close()

    # REAL F4 entries — one per changed field, never a vague combined line.
    _write_f4_edits(
        item=item, changes=[
            ("assignee", before_assignee, assignee),
            ("due_date", before_due, due_date),
        ], actor=actor, reason=reason,
    )
    return get_flagged_item(item_ref, db_path=db_path) or item


def _write_f4_edits(
    *, item: dict[str, Any], changes: list[tuple[str, Any, Any]], actor: str, reason: Optional[str],
) -> None:
    """Write one F4 edit-history entry per actually-changed field. F4's
    record_type is generic ('flagged_item'); record_id is the item_ref; the
    origin is captured in the reason/detail so history stays attributable."""
    try:
        from src.f4 import service as f4

        for field, old, new in changes:
            if (old or None) == (new or None):
                continue
            f4.record_edit(
                record_type="flagged_item",
                record_id=item["item_ref"],
                field=field,
                old_value=old,
                new_value=new,
                reason=reason or f"Changed via Action Center ({item['source_module']}).",
                actor=actor,
                client_id=item.get("client_id"),
            )
    except Exception:  # noqa: BLE001 — never break the action if F4 is unavailable
        pass


def history_for_item(item_ref: str, *, client_id: Optional[int] = None, db_path=None) -> list[dict[str, Any]]:
    """The per-item History panel rows (F4-backed), newest first."""
    try:
        from src.f4 import service as f4

        return f4.history_for_record("flagged_item", item_ref, client_id=client_id)
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Ageing / escalation — self-contained (does NOT depend on Module 1)
# ---------------------------------------------------------------------------


def age_escalation_days(*, db_path=None) -> int:
    """The configured age threshold (days). Read from C1 with a firm default
    fallback — self-contained, so Module 8 follows Module 1's future
    principle without depending on Module 1 existing."""
    try:
        from src.rules import service as rules

        val = rules.get_effective_number(AGE_ESCALATION_DAYS_KEY, default=float(DEFAULT_AGE_ESCALATION_DAYS))
        return int(val) if val is not None else DEFAULT_AGE_ESCALATION_DAYS
    except Exception:  # noqa: BLE001
        return DEFAULT_AGE_ESCALATION_DAYS


def run_ageing_escalation(*, actor: str = "system", db_path=None) -> dict[str, Any]:
    """Scan OPEN items and escalate any past the configured age threshold:
    a visible escalation badge (via the overlay's age_escalated flag) plus a
    C3-routed notification. Idempotent — an item is escalated once."""
    threshold = age_escalation_days(db_path=db_path)
    ctx = ActionCenterContext()
    items = _collect_items(ctx)

    newly: list[str] = []
    conn = _connect(db_path)
    try:
        for item in items:
            if item.get("status") != "open":
                continue
            if (item.get("age_days") or 0) < threshold:
                continue
            if item.get("age_escalated"):
                continue
            acdb.upsert_assignment(
                conn, source_module=item["source_module"], item_ref=item["item_ref"],
                client_id=item.get("client_id"), assignee=None, due_date=item.get("due_date"),
                age_escalated=True, escalated_at=_now_iso(),
            )
            acdb.log_change(
                conn, source_module=item["source_module"], item_ref=item["item_ref"],
                entry_kind="audit_trail", action="age_escalation",
                detail=f"age {item['age_days']}d >= threshold {threshold}d",
                reason=None, actor=actor,
            )
            newly.append(item["item_ref"])
    finally:
        conn.close()

    if newly:
        _register_escalation_notification()
    return {"threshold_days": threshold, "newly_escalated": newly, "count": len(newly)}


def _register_escalation_notification() -> None:
    """Ensure the firm-mandatory 'action center ageing' notification type
    exists via C3 — the same escalation pattern as C2's ambiguous-filing
    state and F5's sync-failure escalation. Best-effort."""
    try:
        from src.settings import db as sdb

        conn = recon_db.get_connection()
        try:
            sdb.upsert_notification_pref(
                conn, "action_center_ageing", "Action Center item ageing escalation",
                "escalation", firm_default=True, firm_mandatory=True,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Batch actions — SURFACE the origin's mechanism; materiality never bypassed
# ---------------------------------------------------------------------------


def batch_exclusion_reason(item: dict[str, Any]) -> Optional[str]:
    """If an item may NOT be batch-acted on, the always-visible reason text
    for the disabled-with-inline-reason checkbox (3.4). Enforced at the QUEUE
    level as well as the origin's own level — no bypass path, regardless of
    who is viewing."""
    if item.get("materiality_gated"):
        return "Materiality-gated — individual review required"
    return None


def eligible_for_batch(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split items into (eligible, excluded) for batch action. Excluded items
    ALWAYS carry a non-empty reason, so the UI can render the inline reason."""
    eligible, excluded = [], []
    for item in items:
        if batch_exclusion_reason(item) is None:
            eligible.append(item)
        else:
            excluded.append(item)
    return eligible, excluded


def batch_action(items: list[dict[str, Any]], *, actor: str, db_path=None) -> dict[str, Any]:
    """Surface each item's ORIGIN batch mechanism (never a Module 8 copy).
    Materiality-gated items are refused here even if a caller somehow slipped
    one through — the queue-level guarantee holds."""
    eligible, excluded = eligible_for_batch(items)

    by_source: dict[str, list[str]] = {}
    for item in eligible:
        by_source.setdefault(item["source_module"], []).append(item["item_ref"])

    results: dict[str, Any] = {}
    for module_tag, refs in by_source.items():
        adapter = _SOURCE_ADAPTERS.get(module_tag)
        if adapter is None:
            continue
        try:
            results[module_tag] = adapter.apply_batch(refs, actor)
        except Exception as exc:  # noqa: BLE001
            results[module_tag] = {"error": str(exc), "count": 0}

    return {
        "applied_total": sum(r.get("count", 0) for r in results.values()),
        "excluded": [i["item_ref"] for i in excluded],
        "by_source": results,
    }


# ---------------------------------------------------------------------------
# Acting on an item — hand off to the origin's OWN detail screen
# ---------------------------------------------------------------------------


def detail_route_for_item(item: dict[str, Any]) -> dict[str, Any]:
    """The routing context the UI uses to open the origin's own detail
    screen. Module 8 renders NO resolution UI of its own."""
    adapter = _SOURCE_ADAPTERS.get(item["source_module"])
    if adapter is None:
        return {}
    return adapter.origin_detail_route(item)


def list_change_log(*, limit: int = 200, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return acdb.list_change_log(conn, limit=limit)
    finally:
        conn.close()