#!/usr/bin/env python
"""Create the multi-user accounts (1 Admin + 3 standard users) for the silo model.

Every credential comes from the ENVIRONMENT — nothing is hardcoded, nothing is
printed. Each new account is created with ``must_change_password`` set, so the
initial password is only ever good for one sign-in.

Usage
-----
    # 1. Name the three users (optional; these are the defaults)
    export SETU_USER1_USERNAME=User1
    export SETU_USER2_USERNAME=User2
    export SETU_USER3_USERNAME=User3

    # 2. Give each one an initial password (>= 8 chars, <= 72 bytes)
    export SETU_USER1_PASSWORD='...'
    export SETU_USER2_PASSWORD='...'
    export SETU_USER3_PASSWORD='...'

    # 3. Run it
    venv/bin/python scripts/seed_users.py

Options
-------
    --role NAME          F1 role for the standard users (default: Senior Accountant)
    --reset-passwords    also replace the password of a user that already exists
    --no-force-change    let these accounts sign in directly, without a forced
                         password change (local/dev convenience only)
    --no-grant-manage    skip granting the clients.profile.manage permission
    --db PATH            target a database other than db/poc.db

A single ``SETU_SEED_USERS="User1:pw1,User2:pw2"`` variable is also accepted
instead of the numbered pairs.

Notes
-----
The three users need ``clients.profile.manage`` in order to create the clients
they are supposed to own, which their default role does not include. Rather
than widening a shared role, the script grants it as a per-user override — so
the change is scoped to exactly these accounts and shows up in the Admin
console's per-user overrides list. Their ACCESS role is 'user', so their
visibility stays siloed to the clients they own whatever else they may do.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import bootstrap  # noqa: E402
from src.auth import access, service as auth  # noqa: E402

DEFAULT_ROLE = "Senior Accountant"
USERS = (1, 2, 3)
PERMISSION_TO_GRANT = "clients.profile.manage"
OVERRIDE_REASON = "Standard user owns clients in the siloed model; needs to create and edit them."


class SeedError(Exception):
    """A problem the operator has to fix before the seed can run."""


def _collect_users() -> list[tuple[str, str]]:
    """``[(username, password)]`` from the environment.

    Passwords are REQUIRED — the script refuses to invent or reuse one, so a
    deployment can never come up holding a credential nobody chose.
    """
    combined = os.environ.get("SETU_SEED_USERS", "").strip()
    if combined:
        pairs: list[tuple[str, str]] = []
        for chunk in combined.split(","):
            if not chunk.strip():
                continue
            username, _, password = chunk.partition(":")
            if not username.strip() or not password:
                raise SeedError(
                    "SETU_SEED_USERS must look like 'User1:pw1,User2:pw2,User3:pw3'."
                )
            pairs.append((username.strip(), password))
        if not pairs:
            raise SeedError("SETU_SEED_USERS was set but contained no entries.")
        return pairs

    collected: list[tuple[str, str]] = []
    missing: list[str] = []
    for index in USERS:
        password = os.environ.get(f"SETU_USER{index}_PASSWORD") or ""
        if not password:
            missing.append(f"SETU_USER{index}_PASSWORD")
            continue
        username = os.environ.get(f"SETU_USER{index}_USERNAME") or f"User{index}"
        collected.append((username, password))
    if missing:
        raise SeedError(
            "Missing credential(s): "
            + ", ".join(missing)
            + ".\nSet them (and optionally SETU_USERn_USERNAME) and run again."
        )
    return collected


def _role_id(role_name: str, *, db_path) -> int:
    roles = auth.list_roles(db_path=db_path)
    for role in roles:
        if role["name"] == role_name:
            return int(role["role_id"])
    raise SeedError(
        f"No role named '{role_name}'. Available: " + ", ".join(r["name"] for r in roles)
    )


def _grant_permission(user_id: int, code: str, *, db_path) -> None:
    permission = next(
        (p for p in auth.list_permissions(db_path=db_path) if p["code"] == code), None
    )
    if permission is None:
        raise SeedError(f"Permission '{code}' is not defined; is the database up to date?")
    auth.set_user_override(
        user_id,
        int(permission["permission_id"]),
        "grant",
        reason=OVERRIDE_REASON,
        actor="seed_users",
        db_path=db_path,
    )


def run(
    *, role_name: str, reset_passwords: bool, grant_manage: bool, force_change: bool, db_path
) -> int:
    bootstrap.init_all(db_path)
    role_id = _role_id(role_name, db_path=db_path)

    existing = {u["username"]: u for u in auth.list_users(db_path=db_path)}
    created = 0
    updated = 0

    for username, password in _collect_users():
        row = existing.get(username)
        if row is None:
            user_id = auth.create_user(
                username=username,
                display_name=username,
                email=None,
                password=password,
                role_id=role_id,
                actor="seed_users",
                must_change_password=force_change,
                db_path=db_path,
            )
            created += 1
            print(f"  + created {username} (role {role_name}, access role 'user')")
        else:
            user_id = int(row["user_id"])
            if reset_passwords:
                auth.reset_user_password(
                    user_id, password, actor="seed_users", require_change=force_change, db_path=db_path
                )
                updated += 1
                print(f"  ~ password reset for {username}")
            else:
                print(f"  = {username} already exists (use --reset-passwords to change it)")

        # Also clear a flag left by an earlier run, so this is the one place that
        # decides whether these accounts are prompted at sign-in.
        auth.set_must_change_password(user_id, force_change, db_path=db_path)

        if grant_manage:
            _grant_permission(user_id, PERMISSION_TO_GRANT, db_path=db_path)

        # An account whose F1 role is Admin/Partner would silently become
        # firm-wide — the opposite of the silo this script sets up. Say so.
        user = auth.get_user(user_id, db_path=db_path) or {}
        if access.viewer_role(user) == access.ADMIN_ROLE:
            print(
                f"    ! {username} has access role 'admin' because of role "
                f"'{user.get('role_name')}'. Give it a standard role for siloed data."
            )

    print(f"\nDone. {created} created, {updated} password(s) reset.")
    if force_change:
        print("Initial passwords are valid for the first sign-in only: each user is")
        print("forced to choose their own before the app opens.")
    else:
        print("--no-force-change: these accounts sign in directly with the password")
        print("given, and are NOT prompted to replace it. Use this for local/dev only —")
        print("a credential that was typed on a command line or pasted into a chat is")
        print("not a secret, so rotate it before any shared or deployed use.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--role", default=os.environ.get("SETU_USER_ROLE") or DEFAULT_ROLE)
    parser.add_argument("--reset-passwords", action="store_true")
    parser.add_argument(
        "--no-force-change",
        action="store_true",
        help="do not require a password change at first sign-in (dev convenience)",
    )
    parser.add_argument("--no-grant-manage", action="store_true")
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    try:
        return run(
            role_name=args.role,
            reset_passwords=args.reset_passwords,
            grant_manage=not args.no_grant_manage,
            force_change=not args.no_force_change,
            db_path=args.db,
        )
    except (SeedError, auth.AuthError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
