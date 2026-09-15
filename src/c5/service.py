"""Public API for the C5 AI Instruction & Knowledge Library module — every
other module (and every UI file) should import from here, not from
src.c5.db directly. Mirrors src/rules/service.py's shape.

Covers: DB init + seed, the instruction library (list/search/entry with
version history), the active/inactive toggle, the proposed-instruction
approval queue, conflict surfacing, and — critically — the runtime context
retrieval that AI touchpoints (F3-AI, Module 2's TDS classification) call to
pull firm-wide + client-scoped instructions alongside their core prompts.

Business rules enforced here (per the C5 build prompt):
- A proposed instruction NEVER goes live without Admin confirmation (no
  automatic path from suggestion to active).
- If two active instructions conflict for the same touchpoint, BOTH are
  surfaced (as conflicting) — never one silently winning.
- An instruction can NEVER direct an AI touchpoint to skip human review or
  auto-apply its own output — a structural, platform-wide invariant.
- Client-specific instructions never leak into another client's context.
- InstructionVersion history is retained INDEFINITELY.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from src import db as recon_db
from src.c5 import db as cdb
from src.c5.schema import init_c5_schema
from src.c5.seed import run_seed


class C5Error(Exception):
    """Raised for expected C5-module failures (validation, blocked action)."""


# Structural invariant: an instruction must never carry wording that would
# let an AI touchpoint skip human review or auto-apply its own output. This
# is enforced on every create/edit/proposal-accept, not just in the UI.
_FORBIDDEN_PHRASES = (
    "skip human review",
    "skip review",
    "auto-apply",
    "auto apply",
    "automatic approval",
    "approve automatically",
    "no human review",
    "without review",
    "unreviewed posting",
)


def _assert_safe_instruction(text: str) -> None:
    """Reject instruction text that tries to bypass human review — the
    platform-wide invariant C5 cannot be used to circumvent."""
    lowered = text.lower()
    for phrase in _FORBIDDEN_PHRASES:
        if phrase in lowered:
            raise C5Error(
                "Instructions can't direct an AI touchpoint to skip human "
                "review or auto-apply its own output."
            )


def init_c5(db_path=None) -> None:
    """Create C5 tables and seed the touchpoint vocabulary. Call once at app
    start, alongside the other module init functions."""
    conn = _connect(db_path)
    try:
        init_c5_schema(conn)
        run_seed(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


# ---------------------------------------------------------------------------
# Touchpoints
# ---------------------------------------------------------------------------


def list_touchpoints(*, include_inactive: bool = True, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.list_touchpoints(conn, include_inactive=include_inactive)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Instruction library
# ---------------------------------------------------------------------------


def list_instructions(*, db_path=None) -> list[dict[str, Any]]:
    """Return all instructions, each enriched with its touchpoint tag keys."""
    conn = _connect(db_path)
    try:
        instructions = cdb.list_instructions(conn)
        for i in instructions:
            i["tags"] = cdb.instruction_tag_keys(conn, i["instruction_id"])
        return instructions
    finally:
        conn.close()


def get_instruction(instruction_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        inst = cdb.get_instruction(conn, instruction_id)
        if inst is not None:
            inst["tags"] = cdb.instruction_tag_keys(conn, instruction_id)
        return inst
    finally:
        conn.close()


def create_instruction(
    *, title: str, explanation: str, scope: str, client_id: Optional[int],
    touchpoint_keys: list[str], actor: str, db_path=None,
) -> int:
    """Create a new instruction (directly active — this is the Admin edit
    path, NOT the proposed path). Enforces the human-review invariant."""
    if not title.strip():
        raise C5Error("Title is required.")
    if not explanation.strip():
        raise C5Error("Explanation is required.")
    if scope not in ("firm", "client"):
        raise C5Error(f"Unknown scope: {scope}")
    if scope == "client" and client_id is None:
        raise C5Error("A client is required for client-scoped instructions.")
    if scope == "firm":
        client_id = None
    _assert_safe_instruction(explanation)

    conn = _connect(db_path)
    try:
        touchpoint_ids = _resolve_touchpoint_ids(conn, touchpoint_keys)
        instruction_id = cdb.create_instruction(
            conn, title=title.strip(), explanation=explanation.strip(),
            scope=scope, client_id=client_id, created_by=actor,
        )
        cdb.set_instruction_tags(conn, instruction_id, touchpoint_ids)
        cdb.insert_instruction_version(
            conn, instruction_id=instruction_id, title=title.strip(),
            explanation=explanation.strip(), scope=scope, client_id=client_id,
            change_summary="Created", changed_by=actor,
        )
        return instruction_id
    finally:
        conn.close()


def edit_instruction(
    instruction_id: int, *, title: str, explanation: str, scope: str,
    client_id: Optional[int], touchpoint_keys: list[str], change_summary: str,
    actor: str, db_path=None,
) -> None:
    """Edit an instruction — appends a NEW immutable InstructionVersion.
    Superseded content is never rewritten; the old version is preserved."""
    if not title.strip():
        raise C5Error("Title is required.")
    if not explanation.strip():
        raise C5Error("Explanation is required.")
    if not change_summary.strip():
        raise C5Error("A change summary is required.")
    _assert_safe_instruction(explanation)

    conn = _connect(db_path)
    try:
        inst = cdb.get_instruction(conn, instruction_id)
        if inst is None:
            raise C5Error("Instruction not found.")
        touchpoint_ids = _resolve_touchpoint_ids(conn, touchpoint_keys)
        cdb.update_instruction(
            conn, instruction_id, title=title.strip(), explanation=explanation.strip(),
            scope=scope, client_id=client_id,
        )
        cdb.set_instruction_tags(conn, instruction_id, touchpoint_ids)
        cdb.insert_instruction_version(
            conn, instruction_id=instruction_id, title=title.strip(),
            explanation=explanation.strip(), scope=scope, client_id=client_id,
            change_summary=change_summary.strip(), changed_by=actor,
        )
    finally:
        conn.close()


def set_instruction_active(instruction_id: int, is_active: bool, *, actor: str, db_path=None) -> None:
    """Active/inactive toggle. Superseding marks inactive; never deletes."""
    conn = _connect(db_path)
    try:
        cdb.set_instruction_active(conn, instruction_id, is_active)
    finally:
        conn.close()


def instruction_versions(instruction_id: int, *, db_path=None) -> list[dict[str, Any]]:
    """Full version history, newest first. Never mutated."""
    conn = _connect(db_path)
    try:
        return cdb.list_instruction_versions(conn, instruction_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Conflict surfacing
# ---------------------------------------------------------------------------


def conflicts_for_touchpoint(touchpoint_key: str, *, db_path=None) -> list[dict[str, Any]]:
    """Return active instructions tagged for a touchpoint. When more than one
    is active for the SAME touchpoint (and same effective scope), the caller
    (Admin review UI) surfaces them side-by-side as conflicting — C5 never
    picks a winner. This function returns the raw candidates; conflict is
    simply len(...) > 1 for a given (touchpoint, scope)."""
    conn = _connect(db_path)
    try:
        tp = cdb.get_touchpoint_by_key(conn, touchpoint_key)
        if tp is None:
            return []
        rows = conn.execute(
            """
            SELECT i.* FROM instructions i
            JOIN instruction_tags it ON it.instruction_id = i.instruction_id
            WHERE it.touchpoint_id = ? AND i.is_active = 1
            ORDER BY i.updated_at DESC
            """,
            (tp["touchpoint_id"],),
        ).fetchall()
        cols = [d[0] for d in conn.execute(
            "SELECT * FROM instructions WHERE 0"
        ).description] or ["instruction_id", "title", "explanation", "scope",
                           "client_id", "is_active", "created_by", "created_at", "updated_at"]
        out = [dict(zip(cols, r)) for r in rows]
        for o in out:
            o["tags"] = cdb.instruction_tag_keys(conn, o["instruction_id"])
        return out
    finally:
        conn.close()


def conflicting_groups(*, db_path=None) -> list[dict[str, Any]]:
    """Return groups of >1 active instruction sharing a (touchpoint, scope),
    for the Admin review-time conflict banner. Each group lists both/all
    conflicting instructions side by side."""
    conn = _connect(db_path)
    try:
        groups: list[dict[str, Any]] = []
        for tp in cdb.list_touchpoints(conn, include_inactive=True):
            rows = conn.execute(
                """
                SELECT i.instruction_id, i.title, i.scope, i.client_id
                FROM instructions i
                JOIN instruction_tags it ON it.instruction_id = i.instruction_id
                WHERE it.touchpoint_id = ? AND i.is_active = 1
                """,
                (tp["touchpoint_id"],),
            ).fetchall()
            # Group by (scope, client_id) to detect same-scope conflicts.
            buckets: dict[tuple, list[dict[str, Any]]] = {}
            for instruction_id, title, scope, client_id in rows:
                key = (scope, client_id)
                buckets.setdefault(key, []).append({"instruction_id": instruction_id, "title": title})
            for (scope, client_id), members in buckets.items():
                if len(members) > 1:
                    groups.append({
                        "touchpoint": tp["label"],
                        "touchpoint_key": tp["key"],
                        "scope": scope,
                        "client_id": client_id,
                        "instructions": members,
                    })
        return groups
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Proposed instructions (submitted from other modules' AI review screens)
# ---------------------------------------------------------------------------


def submit_proposal(
    *, title: str, explanation: str, scope: str, client_id: Optional[int],
    touchpoint_keys: list[str], source: str, submitted_by: str, db_path=None,
) -> int:
    """Submit a proposed instruction from an AI review screen. ALWAYS lands
    as pending — never active. Admin must confirm/edit it separately."""
    if not title.strip() or not explanation.strip():
        raise C5Error("Title and explanation are required.")
    _assert_safe_instruction(explanation)

    conn = _connect(db_path)
    try:
        touchpoint_ids = _resolve_touchpoint_ids(conn, touchpoint_keys)
        proposal_id = cdb.create_proposal(
            conn, title=title.strip(), explanation=explanation.strip(),
            scope=scope, client_id=client_id if scope == "client" else None,
            source=source, submitted_by=submitted_by,
        )
        cdb.set_proposal_tags(conn, proposal_id, touchpoint_ids)
        return proposal_id
    finally:
        conn.close()


def list_proposals(*, status: Optional[str] = "pending", db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        proposals = cdb.list_proposals(conn, status=status)
        for p in proposals:
            p["tags"] = cdb.proposal_tag_keys(conn, p["proposal_id"])
        return proposals
    finally:
        conn.close()


def get_proposal(proposal_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        p = cdb.get_proposal(conn, proposal_id)
        if p is not None:
            p["tags"] = cdb.proposal_tag_keys(conn, proposal_id)
        return p
    finally:
        conn.close()


def approve_proposal(
    proposal_id: int, *, title: Optional[str] = None, explanation: Optional[str] = None,
    touchpoint_keys: Optional[list[str]] = None, actor: str, db_path=None,
) -> int:
    """Admin edit-and-confirm: activate a proposal as a real instruction. The
    Admin may edit title/explanation/tags inline; if omitted, the proposal's
    own text is used. This is the ONLY path from proposal -> active (no
    auto-activation)."""
    conn = _connect(db_path)
    try:
        p = cdb.get_proposal(conn, proposal_id)
        if p is None:
            raise C5Error("Proposal not found.")
        if p["status"] != "pending":
            raise C5Error("This proposal has already been resolved.")

        final_title = (title or p["title"]).strip()
        final_explanation = (explanation or p["explanation"]).strip()
        _assert_safe_instruction(final_explanation)

        tag_keys = touchpoint_keys if touchpoint_keys is not None else cdb.proposal_tag_keys(conn, proposal_id)
        touchpoint_ids = _resolve_touchpoint_ids(conn, tag_keys)

        instruction_id = cdb.create_instruction(
            conn, title=final_title, explanation=final_explanation,
            scope=p["scope"], client_id=p["client_id"], created_by=actor,
        )
        cdb.set_instruction_tags(conn, instruction_id, touchpoint_ids)
        cdb.insert_instruction_version(
            conn, instruction_id=instruction_id, title=final_title,
            explanation=final_explanation, scope=p["scope"], client_id=p["client_id"],
            change_summary=f"Approved from proposal (source: {p['source']})", changed_by=actor,
        )
        cdb.resolve_proposal(conn, proposal_id, status="approved", resolved_by=actor)
        return instruction_id
    finally:
        conn.close()


def reject_proposal(proposal_id: int, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        cdb.resolve_proposal(conn, proposal_id, status="rejected", resolved_by=actor)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Runtime context retrieval (the C5 input slot for F3-AI / Module 2)
# ---------------------------------------------------------------------------


def runtime_context(touchpoint_key: str, *, client_id: Optional[int] = None, db_path=None) -> str:
    """Return the C5 context an AI touchpoint should include alongside its
    core prompt: all active instructions tagged for the touchpoint that are
    firm-wide PLUS the specific client's (when client_id is given). Client-
    specific instructions never leak across clients (a firm-wide instruction
    is the only cross-client item, and it's inherently firm-wide).

    Returns a formatted block of text (or '' if none apply). This is the
    function F3-AI and Module 2's TDS classification call at runtime."""
    conn = _connect(db_path)
    try:
        tp = cdb.get_touchpoint_by_key(conn, touchpoint_key)
        if tp is None:
            return ""
        rows = conn.execute(
            """
            SELECT i.* FROM instructions i
            JOIN instruction_tags it ON it.instruction_id = i.instruction_id
            WHERE it.touchpoint_id = ? AND i.is_active = 1
              AND (i.scope = 'firm' OR (i.scope = 'client' AND i.client_id = ?))
            ORDER BY i.scope ASC, i.updated_at DESC
            """,
            (tp["touchpoint_id"], client_id),
        ).fetchall()
        if not rows:
            return ""
        cols = [d[0] for d in conn.execute("SELECT * FROM instructions WHERE 0").description] \
            or ["instruction_id", "title", "explanation", "scope", "client_id",
                "is_active", "created_by", "created_at", "updated_at"]
        instrs = [dict(zip(cols, r)) for r in rows]
        lines = ["\nFirm guidance (C5 instruction library) — apply alongside the task below:"]
        for i in instrs:
            scope_label = "Firm-wide" if i["scope"] == "firm" else f"Client-specific"
            lines.append(f"- [{scope_label}] {i['title']}: {i['explanation']}")
        return "\n".join(lines)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Optional AI-drafted first-draft wording (assistive only)
# ---------------------------------------------------------------------------


def suggest_wording(pattern: str, *, db_path=None) -> str:
    """OPTIONAL assistive feature: produce a first-draft instruction wording
    from an informally described pattern. Deterministic template-based draft
    (no live model call within C5 itself) — always returned as a DRAFT, never
    saved as active without Admin's explicit edit-and-confirm.

    The draft is deliberately templated so the module stays cost-efficient
    and deterministic, per its PRD ("no AI within the module itself beyond
    one narrow, optional, assistive first-draft-wording feature").
    """
    pattern = (pattern or "").strip()
    if not pattern:
        raise C5Error("Describe the pattern to draft wording for.")
    return (
        f"Treat records matching this pattern as a review priority: "
        f"{pattern}. Confirm against the source data before posting, and "
        "never auto-apply this guidance without a person's explicit review."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_touchpoint_ids(conn: sqlite3.Connection, touchpoint_keys: list[str]) -> list[int]:
    ids: list[int] = []
    for key in touchpoint_keys:
        tp = cdb.get_touchpoint_by_key(conn, key)
        if tp is None:
            raise C5Error(f"Unknown touchpoint: {key}")
        ids.append(tp["touchpoint_id"])
    return ids