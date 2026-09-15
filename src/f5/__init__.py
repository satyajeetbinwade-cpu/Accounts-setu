"""F5 — Data Integrity & Validation Layer.

The trust backbone other modules' AI-assisted steps rely on. Public API
lives in ``src.f5.service`` — everything else (UI, other modules) imports
from there, not from ``src.f5.db`` directly. Mirrors the 3-layer pattern
used by src/auth, src/clients, src/settings, src/rules, src/c5, src/vault,
src/documents.

Depends on F3-AI's canonical output (validates it) and C1's structural
rule definitions (not yet consumed structurally this build — a forward
hook, same posture as other modules that reference C1 rules loosely).
"""
