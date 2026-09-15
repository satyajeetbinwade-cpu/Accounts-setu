"""C3 — System Settings (base) package.

The platform's own control panel: firm profile/branding, notification
preferences, credential/API connection status VIEW (not storage),
onboarding defaults, data retention, locale.

Sub-modules (mirrors src/auth/ and src/clients/):
- schema.py  — SQLite tables, shares db/poc.db.
- db.py      — low-level CRUD.
- service.py — the public API every other module should import.

Key design rule from the C3 build prompt: C3 never stores a secret
itself. Credential/API secrets are entered here but immediately handed
off to C4 (Security & Credential Vault) — until C4 exists, the handoff
is a stub that still renders the masked reference and live status
correctly. C3 only ever shows a masked reference and a live status.
"""

from __future__ import annotations