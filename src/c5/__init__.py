"""C5 — AI Instruction & Knowledge Library package.

An admin-editable, judgment-shaped instruction library that AI touchpoints
read as runtime context alongside their core prompts. Three surfaces: the
instruction library (list/search/entry), the proposed-instruction approval
queue, and per-instruction version history.

Sub-modules (mirrors src/auth/, src/clients/, src/settings/, src/rules/):
- schema.py  — SQLite tables, shares db/poc.db.
- db.py      — low-level CRUD.
- seed.py    — touchpoint vocabulary (two live + placeholder touchpoints).
- service.py — the public API every other module should import.

Key design rules from the C5 build prompt:
- InstructionVersion retains full history INDEFINITELY (not subject to the
  general retention policy — mirrors F3's evidentiary retention exception).
  Superseded instructions are marked inactive, never deleted.
- A ProposedInstruction (submitted by Partner/Manager from another module's
  AI review screen) NEVER goes live without Admin confirmation.
- An instruction can NEVER direct an AI touchpoint to skip human review or
  auto-apply its own output — a structural, platform-wide invariant.
- Client-specific instructions never leak into another client's context.
"""

from __future__ import annotations