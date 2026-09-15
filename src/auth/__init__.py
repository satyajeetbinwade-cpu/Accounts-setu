"""F1 — Login, Access & User Management.

The foundational identity, role, and permission layer every other module
in this PoC authenticates and authorizes against.

Sub-modules:
- security.py  — password hashing, token generation (stdlib only).
- schema.py    — SQLite schema for auth tables (shares db/poc.db with
                  src/db.py, kept in its own module since this is a
                  self-contained, cross-cutting layer).
- seed.py      — first-run seed data: roles, the consolidated permission
                  list, and default role -> permission grants.
- service.py   — the public API every other module should import:
                  authenticate(), session validation, permission checks,
                  user/role/team CRUD, security-event logging.

Nothing outside src/auth and src/ui/auth_ui.py should touch the auth
tables directly — go through service.py so permission logic stays in one
place.
"""
