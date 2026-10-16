"""Guard: a Reflex handler may only assign fields its class DECLARES.

The review screen crashed because ``back_to_files`` assigned
``self._page_srcs`` — a name that does not exist on the state (the field is
``page_srcs``). Reflex raised ``SetUndefinedStateVarError`` INSIDE the handler,
so the handler stopped half-way and left ``documents`` cleared while the index
was still pointing into it; every later update then raised IndexError from a
computed var. Nothing caught it because no test ever ran that handler on a state
with documents loaded.

The first test below is the general net: it scans EVERY class under
``setu/state/`` for ``self.<name> = …`` inside a method where ``<name>`` is not
declared by that class, its ancestors, or (for a mixin) any of its subclasses.
The rest drive ``back_to_files`` and the index-based computed vars directly.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import data_paths
from src import db as recon_db
from src.auth import access
from src.auth import service as auth_service
from src.invoice_extract import demo_seed
from setu.state import invoice_review_state as review_state_mod
from setu.state.invoice_review_state import InvoiceReviewState as S

STATE_DIR = Path(__file__).resolve().parents[1] / "setu" / "state"


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------


def _scan(sources: dict[str, str]) -> list[tuple[str, str, str, str, int]]:
    """(file, class, method, attribute, line) for every undeclared assignment."""
    declared: dict[str, set[str]] = {}
    bases: dict[str, list[str]] = {}
    assignments: list[tuple[str, str, str, str, int]] = []

    for path, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ClassDef):
                continue
            fields = {b.target.id for b in node.body
                      if isinstance(b, ast.AnnAssign) and isinstance(b.target, ast.Name)}
            fields |= {t.id for b in node.body if isinstance(b, ast.Assign)
                       for t in b.targets if isinstance(t, ast.Name)}
            declared[node.name] = fields
            bases[node.name] = (
                [b.id for b in node.bases if isinstance(b, ast.Name)]
                + [b.attr for b in node.bases if isinstance(b, ast.Attribute)]
            )
            for method in node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for sub in ast.walk(method):
                    targets = (
                        sub.targets if isinstance(sub, ast.Assign)
                        else [sub.target] if isinstance(sub, (ast.AnnAssign, ast.AugAssign))
                        else []
                    )
                    for target in targets:
                        if (isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == "self"):
                            assignments.append(
                                (path, node.name, method.name, target.attr, sub.lineno))

    def ancestors(name: str, seen: set[str] | None = None) -> set[str]:
        seen = seen or set()
        if name in seen:
            return set()
        seen.add(name)
        out: set[str] = set()
        for base in bases.get(name, []):
            out |= {base} | ancestors(base, seen)
        return out

    def declared_for(cls: str) -> set[str]:
        out = set(declared.get(cls, set()))
        for parent in ancestors(cls):
            out |= declared.get(parent, set())
        # A MIXIN assigns fields that its subclasses declare (that is what the
        # mixin pattern looks like); those names are declared for it too.
        for other in declared:
            if cls in ancestors(other):
                out |= declared[other]
        return out

    return sorted({
        entry for entry in assignments
        if entry[1] in declared and entry[3] not in declared_for(entry[1])
    })


def test_no_state_handler_assigns_a_field_its_class_does_not_declare():
    """Every `self.x = …` in setu/state/ must name a declared state field."""
    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(STATE_DIR.glob("*.py"))}
    offenders = _scan(sources)
    assert offenders == [], (
        "handlers assign fields their class does not declare "
        "(Reflex raises SetUndefinedStateVarError and the handler stops half-way): "
        + ", ".join(f"{f}:{line} {cls}.{method} -> self.{attr}" for f, cls, method, attr, line in offenders)
    )


def test_the_scanner_catches_the_bug_that_crashed_the_review_screen():
    """The net is only worth having if it catches the real thing: the HEAD
    version of the screen (before the fix) assigns the undeclared `_page_srcs`."""
    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(STATE_DIR.glob("*.py"))}
    sources["invoice_review_state.py"] = sources["invoice_review_state.py"].replace(
        "self.page_srcs = {}", "self._page_srcs = {}", 1)
    offenders = _scan(sources)
    assert [(cls, method, attr) for _f, cls, method, attr, _l in offenders] == [
        ("InvoiceReviewState", "back_to_files", "_page_srcs")]


# ---------------------------------------------------------------------------
# The handler itself, with documents loaded
# ---------------------------------------------------------------------------


@pytest.fixture()
def scratch(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    (root / "data").mkdir(parents=True)
    db_path = root / "scratch.db"
    assert demo_seed.main(["--db", str(db_path)]) == 0
    monkeypatch.setattr(recon_db, "DB_PATH", db_path)
    monkeypatch.setattr(data_paths, "DATA_ROOT", root / "data")
    review_state_mod._RENDER_CACHE.clear()
    monkeypatch.setattr(
        auth_service, "current_user", lambda *_a, **_kw: dict(access.SYSTEM_VIEWER)
    )
    return db_path


def new_state() -> S:
    state = S(_reflex_internal_init=True)
    state.parent_state = SimpleNamespace(
        username="test:guard", session_token="test-session-token",
        viewer_title="", viewer_kind="", viewer_image_src="",
        viewer_page=1, viewer_page_count=0, dirty_substates=set(), dirty_vars=set(),
        _mark_dirty=lambda: None, get_name=lambda: "shim",
    )
    return state


def open_upload(state: S, upload_id: int = 1) -> S:
    state.open_upload_id = upload_id
    state._load_upload()
    state._load_review(first=True)
    return state


def _computed(state: S, name: str):
    """Evaluate a computed var the way the frontend would."""
    value = getattr(state, name)
    if callable(value):
        return value()
    return value


def test_back_to_files_resets_the_index_with_the_lists_it_points_into(scratch):
    """After the crash this handler no longer half-finishes: the index, the
    counts and the lists all end empty, and every computed var that reads them
    answers without raising."""
    state = open_upload(new_state())
    assert state.documents, "the demo scratch database must have documents to open"
    state.current_index = len(state.documents) + 3        # a stale index, as after a crash

    state.back_to_files()

    assert state.open_upload_id == 0
    assert state.documents == [] and state.document_count == 0
    assert state.current_index == 0 and state.current_document_id == 0
    assert state.page_srcs == {}
    for name in ("has_next_issue", "can_go_next", "can_go_prev", "open_ordinal", "review_counter"):
        _computed(state, name)          # must not raise
    assert _computed(state, "has_next_issue") is False
    assert _computed(state, "can_go_next") is False


def test_next_issue_never_indexes_past_a_shorter_document_list(scratch):
    """The follow-on failure: documents cleared, index stale → IndexError."""
    state = open_upload(new_state())
    state.current_index = 2
    state.documents = []                                     # as back_to_files left it
    assert state.has_next_issue is False
    assert state.next_issue() is None                        # the event is a no-op

    # A stale index over a SHORTER list is the same story: it must not raise,
    # and it must answer with an index the list actually has.
    state = open_upload(new_state())
    state.documents = state.documents[:1]
    state.current_index = 99
    index = state._next_issue_index()
    assert index is not None and 0 <= index < len(state.documents)
