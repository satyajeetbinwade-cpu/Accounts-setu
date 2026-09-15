"""C4 — Security & Credential Vault.

Public API lives in ``src.vault.service`` — every other module (C3,
C3-ext once it exists, F1's Recent Security Events panel, C2, F3-AI,
F5, ...) should import from there, never from ``src.vault.db`` directly.
"""
