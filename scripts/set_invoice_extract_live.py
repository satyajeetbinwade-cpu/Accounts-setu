#!/usr/bin/env python3
"""Set the invoice-extraction configuration the Fly database does not carry.

The deploy ships code, not settings. The Fly database therefore comes up with
neither of the two flags, no model assigned to the three touchpoints the v2
pipeline calls, and (until it is set as a Fly secret) no key for the second
read. This script sets exactly the configuration that was TESTED end to end —
nothing is guessed — and it is a dry run unless ``--apply`` is given:

    python scripts/set_invoice_extract_live.py                 # dry run
    python scripts/set_invoice_extract_live.py --apply         # make the changes
    python scripts/set_invoice_extract_live.py --db PATH       # another database
    python scripts/set_invoice_extract_live.py --db PATH --apply

Inside the Fly container it is the same command with ``--db /app/persist/db/poc.db``
(or no ``--db`` at all, which uses the app's own database path).

WHAT IT WRITES, AND HOW. Only through the repo's own functions — the ones the
AI Models screen and ``python -m src.invoice_extract.config`` use — so the
settings rows and the model-assignment change log are written exactly as the
app writes them:

* ``src.settings.service.set_setting_str`` for the two flags;
* ``src.ai_models.service.set_assignment_legs`` for each touchpoint (both legs).

It touches no other table and no other setting. It never creates a database: a
path that does not exist is refused. It never reads, prints or stores a key
value — only whether the environment has one and whether the app's own
``provider_key_configured`` would consider the provider usable.

The desired state below is the tested run (17 Oct 2026, "kmap apr exp.pdf":
31 pages, 21 documents, no hard failures). Both reads of that run were served by
different vendors, which is what the extraction's independence rule requires.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: The actor recorded on every write, so the change log names this script.
ACTOR = "cli:set-invoice-extract-live"

#: The two flags the v2 pipeline watches (extract.KEY_ENABLED and
#: triage.KEY_ENABLED), and the value the tested run had.
FLAGS: tuple[tuple[str, str], ...] = (
    ("invoice_extract.v2.enabled", "true"),
    ("invoice_extract.triage.enabled", "true"),
)

#: (touchpoint, primary provider, primary model, fallback provider, fallback model)
#: exactly as the tested run had them: triage and read A on Gemini through
#: OpenRouter, read B on DeepSeek directly (a different vendor — required),
#: every fallback on ministral through OpenRouter.
ASSIGNMENTS: tuple[tuple[str, str, str, str, str], ...] = (
    ("invoice_triage", "openrouter", "google/gemini-3-flash-preview",
     "openrouter", "mistralai/ministral-3b-2512"),
    ("invoice_extraction_visual", "openrouter", "google/gemini-3-flash-preview",
     "openrouter", "mistralai/ministral-3b-2512"),
    ("invoice_extraction_verify", "deepseek", "deepseek-flash",
     "openrouter", "mistralai/ministral-3b-2512"),
)

#: The providers those legs use and the environment variable each key comes from
#: (the same variable names ``src.ai_models.providers`` declares, which is how a
#: Fly secret reaches the app). A key VALUE is never read into this script.
PROVIDER_KEY_ENVS: tuple[tuple[str, str], ...] = (
    ("openrouter", "OPENROUTER_API_KEY"),
    ("deepseek", "DEEPSEEK_API_KEY"),
)

#: Read A and read B must be served by DIFFERENT vendors, or the extraction
#: refuses the document. Spelled out here so the report can say it plainly.
READ_A_TOUCHPOINT = "invoice_extraction_visual"
READ_B_TOUCHPOINT = "invoice_extraction_verify"


# ---------------------------------------------------------------------------
# Reading the current state (read-only)
# ---------------------------------------------------------------------------


def _open_ro(db_path: Path):
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def current_flags(db_path: Path) -> dict[str, str]:
    """The two flags as stored, or "" when unset."""
    from src.settings import service as settings

    return {
        key: (settings.get_setting_str(key, default=None, db_path=str(db_path)) or "")
        for key, _desired in FLAGS
    }


def current_legs(db_path: Path) -> dict[str, dict]:
    """Each touchpoint's (primary, fallback) legs as stored."""
    from src.ai_models import service as ai_models

    out: dict[str, dict] = {}
    for touchpoint, *_desired in ASSIGNMENTS:
        leg = ai_models.effective_leg(touchpoint, db_path=str(db_path)) or {}
        out[touchpoint] = {
            "primary_provider": (leg.get("primary") or {}).get("provider") or "",
            "primary_model": (leg.get("primary") or {}).get("model") or "",
            "fallback_provider": (leg.get("fallback") or {}).get("provider") or "",
            "fallback_model": (leg.get("fallback") or {}).get("model") or "",
        }
    return out


# ---------------------------------------------------------------------------
# Pre-flight: everything the app would refuse, found BEFORE anything is written
# ---------------------------------------------------------------------------


def preflight(db_path: Path) -> list[str]:
    """The reasons the app would refuse any of these assignments (empty = fine).

    This mirrors the checks ``set_assignment_legs`` performs — a known provider,
    a well-formed model id, two different legs, and (on a vision touchpoint) a
    model the catalogue does not say is text-only — so a refusal is reported
    before a single row changes.
    """
    from src.ai_models import providers as prov
    from src.ai_models import service as ai_models

    problems: list[str] = []
    for touchpoint, pp, pm, fp, fm in ASSIGNMENTS:
        if ai_models.effective_leg(touchpoint, db_path=str(db_path)) is None:
            problems.append(f"{touchpoint}: the target database has no such touchpoint")
            continue
        for provider in (pp, fp):
            try:
                prov.get_spec(provider)
            except Exception as exc:  # noqa: BLE001 — reported, never forced
                problems.append(f"{touchpoint}: provider '{provider}' — {exc}")
        try:
            ai_models.validate_model_id(pm, provider_key=pp)
            ai_models.validate_model_id(fm, provider_key=fp)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{touchpoint}: model id refused — {exc}")
            continue
        if (pp, pm) == (fp, fm):
            problems.append(f"{touchpoint}: the fallback leg equals the primary leg")
        for leg_name, provider, model in (("primary", pp, pm), ("fallback", fp, fm)):
            allowed, reason = ai_models.vision_check(provider, model, db_path=str(db_path))
            if not allowed:
                problems.append(f"{touchpoint} ({leg_name}): {reason}")
    return problems


def catalogue_notes(db_path: Path) -> list[str]:
    """Which desired models this database's catalogue does not list.

    An unknown model is ALLOWED by the app (we never refuse what cannot be
    proven), so this is a note, not a blocker — and nothing is inserted here:
    the catalogue is the app's cache to fill, not this script's.
    """
    from src.ai_models import service as ai_models

    notes: list[str] = []
    seen: set[tuple[str, str]] = set()
    for _touchpoint, pp, pm, fp, fm in ASSIGNMENTS:
        for provider, model in ((pp, pm), (fp, fm)):
            if (provider, model) in seen:
                continue
            seen.add((provider, model))
            try:
                modality = ai_models.model_modality(provider, model, db_path=str(db_path))
            except Exception:  # noqa: BLE001 — a catalogue read must not fail the run
                modality = None
            if modality is None:
                notes.append(
                    f"{provider}/{model}: not in this database's model catalogue "
                    "(unknown modality — the app allows it)"
                )
    return notes


# ---------------------------------------------------------------------------
# The plan, and applying it
# ---------------------------------------------------------------------------


def _leg_text(legs: dict) -> str:
    if not any(legs.values()):
        return "(unset)"
    return (
        f"{legs['primary_provider']}/{legs['primary_model']} → "
        f"{legs['fallback_provider']}/{legs['fallback_model']}"
    )


def plan(db_path: Path) -> list[tuple[str, str, str, bool]]:
    """(name, current, desired, needs_change) for every setting this script owns."""
    flags = current_flags(db_path)
    legs = current_legs(db_path)
    rows: list[tuple[str, str, str, bool]] = []
    for key, desired in FLAGS:
        current = flags.get(key, "")
        rows.append((key, current or "(unset)", desired, current != desired))
    for touchpoint, pp, pm, fp, fm in ASSIGNMENTS:
        desired = f"{pp}/{pm} → {fp}/{fm}"
        current = _leg_text(legs.get(touchpoint) or {})
        rows.append((touchpoint, current, desired, current != desired))
    return rows


def _print_table(rows: list[tuple[str, str, str, bool]]) -> None:
    width = max(len(row[0]) for row in rows)
    for name, current, desired, needs_change in rows:
        status = "will change" if needs_change else "ok"
        print(f"  {name.ljust(width)}  current: {current}")
        print(f"  {' ' * width}  desired: {desired}   [{status}]")


def apply_plan(db_path: Path, rows: list[tuple[str, str, str, bool]]) -> list[str]:
    """Make only the changes the plan says are needed. Returns the errors."""
    from src.ai_models import service as ai_models
    from src.settings import service as settings

    errors: list[str] = []
    desired_flags = dict(FLAGS)
    desired_assign = {touchpoint: (pp, pm, fp, fm) for touchpoint, pp, pm, fp, fm in ASSIGNMENTS}

    for name, _current, _desired, needs_change in rows:
        if not needs_change:
            continue
        try:
            if name in desired_flags:
                settings.set_setting_str(
                    name, desired_flags[name], actor=ACTOR, db_path=str(db_path),
                )
            else:
                pp, pm, fp, fm = desired_assign[name]
                ai_models.set_assignment_legs(
                    name, primary_provider=pp, primary_model=pm,
                    fallback_provider=fp, fallback_model=fm,
                    actor=ACTOR, db_path=str(db_path),
                )
        except Exception as exc:  # noqa: BLE001 — reported by name, never forced
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return errors


# ---------------------------------------------------------------------------
# The provider-key report (never a key value)
# ---------------------------------------------------------------------------


def key_report(db_path: Path) -> list[str]:
    """For each provider: is its key in the environment, and is it usable?

    The environment is only ASKED whether the variable is set; the value is
    never read into this process's output. ``provider_key_configured`` is the
    app's own check (vault first, then environment) and reports the source.
    """
    import os

    from src.ai_models import service as ai_models

    lines: list[str] = []
    for provider, env_var in PROVIDER_KEY_ENVS:
        present = bool(os.environ.get(env_var))
        try:
            configured, source = ai_models.provider_key_configured(
                provider, db_path=str(db_path),
            )
        except Exception as exc:  # noqa: BLE001
            configured, source = False, f"check failed: {exc}"
        usable = f"usable ({source})" if configured else "NOT configured"
        lines.append(
            f"  {provider:<11} {env_var} present in environment: "
            f"{'yes' if present else 'no'} — app considers it {usable}"
        )
    return lines


def read_a_b_report(legs: dict[str, dict]) -> str:
    """Do read A and read B resolve to different vendors? The extraction refuses
    a second read served by the same vendor, so say it before anyone deploys.

    Judged on the configuration this script SETS (that is what the database
    will hold afterwards); the stored state is mentioned only when it differs.
    """
    desired_a = next(pp for t, pp, _pm, _fp, _fm in ASSIGNMENTS if t == READ_A_TOUCHPOINT)
    desired_b = next(pp for t, pp, _pm, _fp, _fm in ASSIGNMENTS if t == READ_B_TOUCHPOINT)
    if desired_a == desired_b:
        return (
            f"WARNING: read A and read B both resolve to '{desired_a}' — the extraction "
            "refuses a second read from the same provider."
        )
    line = f"read A on '{desired_a}', read B on '{desired_b}' — independent providers, as required."
    current_a = (legs.get(READ_A_TOUCHPOINT) or {}).get("primary_provider") or ""
    current_b = (legs.get(READ_B_TOUCHPOINT) or {}).get("primary_provider") or ""
    if current_a and current_a == current_b:
        line += f" (stored now: both '{current_a}' — applying this fixes that.)"
    return line


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _default_db_path() -> Path:
    from src import db as recon_db

    return Path(recon_db.DB_PATH)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/set_invoice_extract_live.py",
        description=(
            "Set the tested invoice-extraction configuration on a database. "
            "Dry run unless --apply is given."
        ),
    )
    parser.add_argument(
        "--db", default=None,
        help="database to change (default: the app's own database path)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="make the changes (without this the script only prints)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser() if args.db else _default_db_path()
    print(f"database: {db_path}")
    print(f"mode:     {'APPLY' if args.apply else 'DRY RUN (nothing will be written)'}")
    if not db_path.exists():
        print(f"refusing: {db_path} does not exist — this script never creates a database.")
        return 2

    try:
        _open_ro(db_path).close()
    except Exception as exc:  # noqa: BLE001
        print(f"refusing: {db_path} is not a readable database ({exc}).")
        return 2

    problems = preflight(db_path)
    if problems:
        print("refusing: the app would refuse this configuration, so nothing was changed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("provider keys (values are never read or printed):")
    for line in key_report(db_path):
        print(line)

    rows = plan(db_path)
    print("read A / read B:", read_a_b_report(current_legs(db_path)))
    notes = catalogue_notes(db_path)
    if notes:
        print("model catalogue notes:")
        for note in notes:
            print(f"  - {note}")

    print("settings:")
    _print_table(rows)
    changes = [row[0] for row in rows if row[3]]
    if not args.apply:
        print(
            f"dry run: {len(changes)} change(s) needed"
            + (f" ({', '.join(changes)})" if changes else "")
            + " — nothing was written. Re-run with --apply to make them."
        )
        return 0

    errors = apply_plan(db_path, rows)
    if errors:
        print("errors (the app refused these; nothing was forced):")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("after --apply:")
    _print_table(plan(db_path))
    print(f"applied: {len(changes)} change(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
