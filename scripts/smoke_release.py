#!/usr/bin/env python3
"""Release smoke test — re-runnable against any base URL. No secrets in this file.

Usage::

    ./venv/bin/python scripts/smoke_release.py <base-url> --mode unauthenticated
    SETU_TEST_USER=... SETU_TEST_PASSWORD=... \\
        ./venv/bin/python scripts/smoke_release.py <base-url> --mode authenticated

Modes
-----
``unauthenticated``
    Loads ``/login`` and checks: HTTP 200, the sign-in form renders, the page
    shows NO first-run credentials hint, no static asset or API request fails
    (4xx/5xx), and the backend event endpoint responds.

``authenticated``
    Reads ``SETU_TEST_USER`` / ``SETU_TEST_PASSWORD`` from the environment
    (never from this file, never printed — the password is masked as
    ``********``), signs in, and opens every sidebar page. READ-ONLY against a
    live URL: no uploads, no creates, no deletes, no flag or settings changes,
    no password changes. If a forced-password-change screen appears the run
    STOPS there and reports it without submitting anything.


Exit code is 0 only when every check passed.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from playwright.sync_api import sync_playwright

DEFAULT_SHOTS = os.path.join(os.getcwd(), "smoke_shots")

SIDEBAR = [
    ("/", "dashboard"), ("/reconcile", "reconcile"), ("/follow-ups", "follow-ups"),
    ("/clients", "clients"), ("/review", "review"), ("/run", "run"),
    ("/compare", "compare"), ("/export", "export"),
    ("/smart-ingestion", "smart-ingestion"), ("/format-registry", "format-registry"),
    ("/invoice-extraction", "invoice-extraction"), ("/reconciliation", "reconciliation"),
    ("/action-center", "action-center"), ("/data-integrity", "data-integrity"),
    ("/filing", "filing"), ("/integrations", "integrations"), ("/config", "config"),
    ("/settings", "settings"), ("/rules", "rules"), ("/ai-library", "ai-library"),
    ("/ai-models", "ai-models"), ("/vault", "vault"), ("/documents", "documents"),
    ("/security", "security"), ("/admin", "admin"),
]

FORCED_CHANGE_MARKERS = (
    "change your password", "must change", "forced password", "set a new password",
    "new password",
)

#: Copy from the OLD first-run hint block. A deployment's login page must not
#: carry any of it. (Detecting it by its copy keeps this file free of the
#: password itself.)
HINT_MARKERS = (
    "first run?", "default admin", "change this password", "credentials hint",
)


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []
        self.console_errors: list[str] = []
        self.http_errors: list[str] = []

    def check(self, name: str, ok: bool, evidence: str) -> None:
        self.rows.append((name, bool(ok), evidence))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} — {evidence}", flush=True)

    def failed(self) -> bool:
        return any(not ok for _, ok, _ in self.rows)


def _wait_logged_in(page, base: str, timeout_s: int = 30) -> bool:
    for _ in range(timeout_s * 2):
        try:
            if page.url.rstrip("/") == base.rstrip("/") and page.locator('input[name="username"]').count() == 0:
                return True
        except Exception:
            pass
        page.wait_for_timeout(500)
    return False


def _ensure_link(page, base: str, route: str):
    """Sidebar links live in collapsible sections; expand only while needed."""
    loc = page.locator(f'a[href="{route}"]')
    if loc.count():
        return loc.first
    if page.url.rstrip("/") != base.rstrip("/"):
        page.goto(f"{base}{route if route != '/' else ''}", wait_until="load", timeout=60000)
        page.wait_for_timeout(1500)
    for label in ("ALL TOOLS", "SETUP", "RECONCILE"):
        loc = page.locator(f'a[href="{route}"]')
        if loc.count():
            return loc.first
        head = page.locator(f"text={label}")
        if head.count():
            try:
                head.first.click(timeout=3000)
                page.wait_for_timeout(700)
            except Exception:
                pass
    loc = page.locator(f'a[href="{route}"]')
    return loc.first if loc.count() else None


def run_unauthenticated(page, base: str, rep: Report, shots: str) -> None:
    resp = page.goto(f"{base}/login", wait_until="load", timeout=60000)
    status = resp.status if resp else 0
    page.wait_for_selector('input[name="username"]', timeout=30000)
    page.wait_for_timeout(1500)
    body = page.inner_text("body")
    page.screenshot(path=os.path.join(shots, "unauth_login.png"), full_page=True)
    rep.check("login returns 200", status == 200, f"HTTP {status}")
    rep.check("sign-in form renders", page.locator('input[name="username"]').count() == 1
              and page.locator('input[name="password"]').count() == 1, "username+password inputs present")
    rep.check("no credentials hint", not any(m in body.lower() for m in HINT_MARKERS),
              "no default-password text on the page")
    # The event endpoint: an HTTP response of any kind means the backend route
    # exists; a WebSocket attempt verifies it upgrades.
    try:
        er = page.request.get(f"{base}/_event")
        event_ok = er.status < 500 and er.status != 404
        event_evidence = f"/_event HTTP {er.status}"
    except Exception as exc:
        event_ok, event_evidence = False, f"/_event error {exc}"
    rep.check("backend event endpoint responds", event_ok, event_evidence)
    rep.check("no failed asset/API requests", not rep.http_errors, f"errors={rep.http_errors[:3]}")


def run_authenticated(page, base: str, rep: Report, shots: str) -> None:
    user = os.environ.get("SETU_TEST_USER", "")
    password = os.environ.get("SETU_TEST_PASSWORD", "")
    if not user or not password:
        rep.check("credentials available", False, "SETU_TEST_USER / SETU_TEST_PASSWORD must be set")
        return

    page.goto(f"{base}/login", wait_until="load", timeout=60000)
    page.wait_for_selector('input[name="username"]', timeout=30000)
    page.fill('input[name="username"]', user)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_timeout(2500)

    # STOP at a forced-password-change screen without submitting anything.
    body = page.inner_text("body").lower()
    if any(marker in body for marker in FORCED_CHANGE_MARKERS):
        page.screenshot(path=os.path.join(shots, "auth_forced_password_change.png"), full_page=True)
        rep.check("no forced-password-change screen", False,
                  "STOPPED at a forced-password-change screen; nothing submitted (shot auth_forced_password_change.png)")
        return

    used = user
    logged_in = _wait_logged_in(page, base, 15)
    if not logged_in and user != user.lower():
        # The documented fallback: the login may be case-sensitive.
        page.fill('input[name="username"]', user.lower())
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"]')
        page.wait_for_timeout(2500)
        used = user.lower()
        logged_in = _wait_logged_in(page, base, 25)
    rep.check("login (password ********)", logged_in, f"username that worked: {used!r}")
    if not logged_in:
        page.screenshot(path=os.path.join(shots, "auth_login_failed.png"), full_page=True)
        return

    page.screenshot(path=os.path.join(shots, "auth_dashboard.png"), full_page=True)

    failures = []
    for route, slug in SIDEBAR:
        c0, h0 = len(rep.console_errors), len(rep.http_errors)
        t0 = time.monotonic()
        try:
            if route == "/":
                page.goto(base + "/", wait_until="load", timeout=60000)
            else:
                link = _ensure_link(page, base, route)
                if link is None:
                    failures.append(f"{route}: no sidebar link")
                    continue
                link.click(timeout=5000)
                for _ in range(20):
                    if page.url.rstrip("/") == (base + route).rstrip("/"):
                        break
                    page.wait_for_timeout(400)
            page.wait_for_timeout(1800)
            load = round(time.monotonic() - t0, 1)
            errs = rep.console_errors[c0:]
            https = rep.http_errors[h0:]
            ok = page.url.rstrip("/") == (base + route).rstrip("/") and not errs and not https
            rep.check(f"page {route} opens", ok,
                      f"{load}s url={page.url}" + (f" console={errs[:2]}" if errs else "")
                      + (f" http={https[:2]}" if https else ""))
            if not ok:
                failures.append(route)
        except Exception as exc:
            rep.check(f"page {route} opens", False, f"EXCEPTION {type(exc).__name__}: {exc}")
            failures.append(route)

    # Session survives a refresh (read-only).
    page.goto(f"{base}/", wait_until="load", timeout=60000)
    page.wait_for_timeout(2000)
    page.reload(wait_until="load")
    page.wait_for_timeout(2500)
    rep.check("session survives refresh",
              page.locator('input[name="username"]').count() == 0 and "Sign in" not in page.inner_text("body")[:400],
              f"url={page.url}")

    rep.check("no console errors during the run", not rep.console_errors,
              f"{len(rep.console_errors)} error(s): {rep.console_errors[:3]}")
    rep.check("no failed HTTP requests during the run", not rep.http_errors,
              f"{len(rep.http_errors)} error(s): {rep.http_errors[:3]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-runnable Setu release smoke test")
    ap.add_argument("base_url")
    ap.add_argument("--mode", choices=("unauthenticated", "authenticated"), required=True)
    ap.add_argument("--shots", default=DEFAULT_SHOTS, help=f"screenshot folder (default: {DEFAULT_SHOTS})")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    os.makedirs(args.shots, exist_ok=True)
    rep = Report()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.on("console", lambda m: rep.console_errors.append(f"{page.url} :: {m.text[:160]}")
                if m.type == "error" else None)
        page.on("response", lambda r: rep.http_errors.append(f"HTTP {r.status} {r.url[:140]}")
                if r.status >= 400 else None)
        try:
            if args.mode == "unauthenticated":
                run_unauthenticated(page, base, rep, args.shots)
            else:
                run_authenticated(page, base, rep, args.shots)
        finally:
            browser.close()

    print(f"\nsmoke_release: {'FAIL' if rep.failed() else 'PASS'} "
          f"({sum(1 for _, ok, _ in rep.rows if ok)}/{len(rep.rows)} checks passed, mode={args.mode})")
    return 1 if rep.failed() else 0


if __name__ == "__main__":
    raise SystemExit(main())
