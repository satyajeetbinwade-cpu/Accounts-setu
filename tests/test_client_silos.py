"""Task 2 — per-user data silos, enforced server-side.

Pins the guarantees a standard user cannot get around:

* their client list contains ONLY the clients they own — never another user's;
* asking for another user's client by id is refused (403/404), whether through
  the strict lookup or the render-path lookup;
* the Admin roster filter cannot be used by a standard user to widen anything;
* child records (contacts, documents) of another user's client are refused;
* mutations on another user's client are refused;
* the Admin sees everything;
* a client created through the service is owned by its creator, from the
  session rather than from any request input.

These are the SERVICE-layer guarantees. The Reflex states pass the signed-in
viewer into these calls, and the signatures make the viewer mandatory — so a
page that forgot to scope cannot compile, let alone leak.

Run:

    venv/bin/python -m pytest tests/test_client_silos.py -v
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import bootstrap  # noqa: E402
from src.auth import access  # noqa: E402
from src.auth import service as auth  # noqa: E402
from src.clients import service as clients  # noqa: E402
from src.documents import service as documents  # noqa: E402
from src.shared import discovery  # noqa: E402


@pytest.fixture(scope="session")
def _seeded_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("silo_seed") / "poc.db"
    bootstrap.run_migrations(path)
    return path


@pytest.fixture()
def db_path(_seeded_db, tmp_path):
    dest = tmp_path / "poc.db"
    shutil.copy(_seeded_db, dest)
    return str(dest)


@pytest.fixture()
def world(db_path):
    """Two standard users, one Admin, and one client each.

    User1's client is deliberately named "Test Client" and User2's
    "Meridian Fabrics" — the two folders that really exist under ``data/`` — so
    the filesystem-derived client list can be exercised too.
    """
    staff_role = int(
        next(r for r in auth.list_roles(db_path=db_path) if r["name"] == "Senior Accountant")[
            "role_id"
        ]
    )
    user1_id = auth.create_user(
        username="User1", display_name="User1", email=None, password="user1-password",
        role_id=staff_role, actor="test", db_path=db_path,
    )
    user2_id = auth.create_user(
        username="User2", display_name="User2", email=None, password="user2-password",
        role_id=staff_role, actor="test", db_path=db_path,
    )
    admin_id = int(
        next(u for u in auth.list_users(db_path=db_path) if u["username"] == "admin")["user_id"]
    )

    user1 = auth.get_user(user1_id, db_path=db_path)
    user2 = auth.get_user(user2_id, db_path=db_path)
    admin = auth.get_user(admin_id, db_path=db_path)

    client1 = clients.create_client(
        legal_name="Test Client", pan="AAACT1234C", actor="User1",
        owner_user_id=user1_id, db_path=db_path,
    )
    client2 = clients.create_client(
        legal_name="Meridian Fabrics", pan="AABCM1234K", actor="User2",
        owner_user_id=user2_id, db_path=db_path,
    )
    client_admin = clients.create_client(
        legal_name="Admin Co", pan="AAACA1234D", actor="admin",
        owner_user_id=admin_id, db_path=db_path,
    )

    return {
        "db_path": db_path,
        "user1": user1,
        "user2": user2,
        "admin": admin,
        "client1": client1,
        "client2": client2,
        "client_admin": client_admin,
    }


# ---------------------------------------------------------------------------
# (b) A standard user sees only their own clients
# ---------------------------------------------------------------------------


def test_standard_user_list_contains_only_their_own_clients(world):
    rows = clients.list_clients(viewer=world["user1"], db_path=world["db_path"])
    names = [r["legal_name"] for r in rows]

    assert names == ["Test Client"]
    assert "Meridian Fabrics" not in names
    assert "Admin Co" not in names


def test_standard_user_count_never_includes_other_users(world):
    """Counts, not just rows: a dashboard's "N clients" must not leak."""
    user1 = clients.list_clients(viewer=world["user1"], db_path=world["db_path"])
    admin = clients.list_clients(viewer=world["admin"], db_path=world["db_path"])

    assert len(user1) == 1
    assert len(admin) == 3


def test_standard_user_cannot_widen_with_the_admin_owner_filter(world):
    """``filter_owner_id`` is an Admin narrowing tool. For a standard user it
    is ignored — their own scope always wins."""
    rows = clients.list_clients(
        viewer=world["user1"],
        filter_owner_id=int(world["admin"]["user_id"]),
        db_path=world["db_path"],
    )
    assert [r["legal_name"] for r in rows] == ["Test Client"]


def test_missing_viewer_fails_closed(world):
    """No viewer is NOT "see everything" — it is not-your-record."""
    assert clients.list_clients(viewer=None, db_path=world["db_path"]) == []
    assert clients.list_clients(viewer={"user_id": 0}, db_path=world["db_path"]) == []


# ---------------------------------------------------------------------------
# (c) The Admin sees everything
# ---------------------------------------------------------------------------


def test_admin_sees_all_clients_with_their_owners(world):
    rows = clients.list_clients(viewer=world["admin"], db_path=world["db_path"])
    by_name = {r["legal_name"]: r for r in rows}

    assert set(by_name) == {"Test Client", "Meridian Fabrics", "Admin Co"}
    assert by_name["Test Client"]["owner_user_id"] == world["user1"]["user_id"]
    assert by_name["Meridian Fabrics"]["owner_user_id"] == world["user2"]["user_id"]


def test_admin_can_filter_the_roster_by_owner(world):
    rows = clients.list_clients(
        viewer=world["admin"],
        filter_owner_id=int(world["user2"]["user_id"]),
        db_path=world["db_path"],
    )
    assert [r["legal_name"] for r in rows] == ["Meridian Fabrics"]


def test_owner_options_list_only_users_who_own_clients(world):
    owners = clients.list_client_owners(db_path=world["db_path"])
    usernames = {o["username"] for o in owners}

    assert usernames == {"User1", "User2", "admin"}


# ---------------------------------------------------------------------------
# (d) Direct access by id to another user's record is refused
# ---------------------------------------------------------------------------


def test_get_another_users_client_by_id_is_refused(world):
    with pytest.raises(clients.ClientAccessDenied):
        clients.get_client(world["client2"], viewer=world["user1"], db_path=world["db_path"])


def test_refusal_does_not_reveal_whether_the_record_exists(world):
    """A real-but-not-mine id and a nonexistent id must answer identically."""
    with pytest.raises(clients.ClientAccessDenied) as other_users:
        clients.get_client(world["client2"], viewer=world["user1"], db_path=world["db_path"])
    with pytest.raises(clients.ClientAccessDenied) as nonexistent:
        clients.get_client(999_999, viewer=world["user1"], db_path=world["db_path"])

    assert str(other_users.value) == str(nonexistent.value)


def test_render_path_lookup_returns_none_for_another_users_client(world):
    assert clients.find_client(world["client2"], viewer=world["user1"], db_path=world["db_path"]) is None
    assert clients.find_client(world["client1"], viewer=world["user1"], db_path=world["db_path"]) is not None


def test_admin_can_open_any_client_by_id(world):
    assert clients.get_client(world["client2"], viewer=world["admin"], db_path=world["db_path"])[
        "legal_name"
    ] == "Meridian Fabrics"


def test_standard_user_cannot_mutate_another_users_client(world):
    with pytest.raises(clients.ClientAccessDenied):
        clients.update_client_details(
            world["client2"], actor="User1", viewer=world["user1"],
            legal_name="Hijacked", db_path=world["db_path"],
        )
    with pytest.raises(clients.ClientAccessDenied):
        clients.set_client_active(
            world["client2"], False, actor="User1", viewer=world["user1"], db_path=world["db_path"]
        )
    with pytest.raises(clients.ClientAccessDenied):
        clients.update_client_field(
            world["client2"], "legal_name", "Hijacked", actor="User1", viewer=world["user1"],
            db_path=world["db_path"],
        )

    # ...and nothing changed.
    still_there = clients.get_client(world["client2"], viewer=world["admin"], db_path=world["db_path"])
    assert still_there["legal_name"] == "Meridian Fabrics"
    assert still_there["is_active"] == 1


# ---------------------------------------------------------------------------
# Filesystem / runs view (the client pickers)
# ---------------------------------------------------------------------------


def test_discovery_client_list_is_siloed(world):
    db = world["db_path"]

    def names(viewer):
        return [
            c["legal_name"]
            for c in discovery.list_clients(viewer=viewer, db_path=db)
        ]

    assert names(world["user1"]) == ["Test Client"]
    assert names(world["user2"]) == ["Meridian Fabrics"]
    # The admin's list also carries "Admin Co" — a client RECORD with no data/
    # folder — because options come from the client table (the single source of
    # truth), so a client is selectable from the moment it is created rather
    # than only once something has been uploaded for it.
    assert names(world["admin"]) == ["Admin Co", "Meridian Fabrics", "Test Client"]
    # An ownerless/faceless viewer sees nothing at all.
    assert discovery.list_clients(viewer=None, db_path=db) == []


def test_a_just_created_client_is_immediately_selectable(world):
    """The guided flow's whole point: create a client, and it is in the picker
    straight away — before any period or source file exists for it."""
    created = clients.create_client(
        legal_name="Brand New Client",
        pan="AAABN1234E",
        actor="User1",
        owner_user_id=int(world["user1"]["user_id"]),
        db_path=world["db_path"],
    )
    assert created > 0

    assert [
        c["legal_name"]
        for c in discovery.list_clients(viewer=world["user1"], db_path=world["db_path"])
    ] == [
        "Brand New Client",
        "Test Client",
    ]
    # ...and it is still invisible to everybody else.
    assert "Brand New Client" not in [
        c["legal_name"]
        for c in discovery.list_clients(viewer=world["user2"], db_path=world["db_path"])
    ]


def test_unregistered_folder_is_never_listed(db_path, tmp_path, monkeypatch):
    """A folder with no owning client row must not leak to a standard user.

    Options come from the client table, so a folder with no record is listed
    for NOBODY — including an admin. A tree that predates ownership becomes
    visible only by being registered (``scripts/migrate_ownership.py``), which
    is what keeps an unowned folder from silently widening anybody's view.
    """
    from src.shared import discovery as disc

    root = tmp_path / "data"
    (root / "Ghost Client").mkdir(parents=True)
    monkeypatch.setattr(disc, "DATA_ROOT", root)

    staff_role = int(
        next(r for r in auth.list_roles(db_path=db_path) if r["name"] == "Senior Accountant")[
            "role_id"
        ]
    )
    user_id = auth.create_user(
        username="User9", display_name="User9", email=None, password="user9-password",
        role_id=staff_role, actor="test", db_path=db_path,
    )
    user = auth.get_user(user_id, db_path=db_path)

    assert disc.list_clients(viewer=user, db_path=db_path) == []
    assert disc.list_clients(viewer=access.SYSTEM_VIEWER, db_path=db_path) == []


# ---------------------------------------------------------------------------
# Child records inherit the client's silo
# ---------------------------------------------------------------------------


def test_child_records_of_another_users_client_are_refused(world):
    contact_id = clients.add_contact(
        world["client2"], name="Their Accountant", role_title=None, email=None, phone=None,
        notes=None, actor="User2", viewer=world["user2"], db_path=world["db_path"],
    )

    with pytest.raises(clients.ClientAccessDenied):
        clients.list_contacts(world["client2"], viewer=world["user1"], db_path=world["db_path"])
    with pytest.raises(clients.ClientAccessDenied):
        clients.remove_contact(contact_id, actor="User1", viewer=world["user1"], db_path=world["db_path"])

    # The owner still has it.
    assert len(clients.list_contacts(world["client2"], viewer=world["user2"], db_path=world["db_path"])) == 1
    assert len(clients.list_contacts(world["client2"], viewer=world["admin"], db_path=world["db_path"])) == 1


def test_child_owner_mirrors_the_parent_client(world):
    """The denormalised owner is set from the parent, not from the caller."""
    clients.add_contact(
        world["client1"], name="My Accountant", role_title=None, email=None, phone=None,
        notes=None, actor="User1", viewer=world["user1"], db_path=world["db_path"],
    )
    contacts = clients.list_contacts(world["client1"], viewer=world["admin"], db_path=world["db_path"])
    assert contacts[0]["owner_user_id"] == world["user1"]["user_id"]


def test_documents_are_siloed(world):
    owned = documents.upload_document(
        client_id=world["client1"], filename="GSTR-2B_Aug.xlsx", file_bytes=b"a,b\n1,2\n",
        period="2026-08", actor="User1", viewer=world["user1"], db_path=world["db_path"],
    )
    theirs = documents.upload_document(
        client_id=world["client2"], filename="Tally_Aug.xlsx", file_bytes=b"x,y\n1,2\n",
        period="2026-08", actor="User2", viewer=world["user2"], db_path=world["db_path"],
    )

    user1_docs = documents.list_documents(viewer=world["user1"], db_path=world["db_path"])
    assert [d["document_id"] for d in user1_docs] == [owned["document_id"]]

    admin_docs = documents.list_documents(viewer=world["admin"], db_path=world["db_path"])
    assert {d["document_id"] for d in admin_docs} == {owned["document_id"], theirs["document_id"]}

    with pytest.raises(documents.DocumentAccessDenied):
        documents.get_document(theirs["document_id"], viewer=world["user1"], db_path=world["db_path"])
    assert documents.find_document(theirs["document_id"], viewer=world["user1"], db_path=world["db_path"]) is None
    assert documents.find_document(owned["document_id"], viewer=world["user1"], db_path=world["db_path"]) is not None


def test_review_queue_counts_are_scoped(world):
    # "mystery_document.pdf" classifies at 45% confidence, so it is routed to
    # the review queue — the count whose scoping this test pins.
    documents.upload_document(
        client_id=world["client2"], filename="mystery_document.pdf", file_bytes=b"%PDF-1.4",
        period=None, actor="User2", viewer=world["user2"], db_path=world["db_path"],
    )

    assert documents.queue_progress(viewer=world["user1"], db_path=world["db_path"])["pending"] == 0
    assert documents.queue_progress(viewer=world["admin"], db_path=world["db_path"])["pending"] >= 1
    assert documents.list_queue("pending", viewer=world["user1"], db_path=world["db_path"]) == []


# ---------------------------------------------------------------------------
# Ownership is taken from the session, not from request input
# ---------------------------------------------------------------------------


def test_new_client_is_owned_by_its_creator(world):
    created = clients.create_client(
        legal_name="Fresh Client", pan="AAACF1234F", actor="User1",
        owner_user_id=int(world["user1"]["user_id"]),
        db_path=world["db_path"],
    )
    row = clients.get_client(created, viewer=world["admin"], db_path=world["db_path"])
    assert row["owner_user_id"] == world["user1"]["user_id"]

    assert clients.find_client(created, viewer=world["user2"], db_path=world["db_path"]) is None
    assert [c["legal_name"] for c in clients.list_clients(viewer=world["user1"], db_path=world["db_path"])] == [
        "Fresh Client",
        "Test Client",
    ]


# ---------------------------------------------------------------------------
# Scoping helpers shared by the multi-client queues (Action Center, filing
# calendar, dashboard counts)
# ---------------------------------------------------------------------------


def test_owned_client_ids_is_the_id_keyed_twin_of_the_name_set(world):
    ids = access.owned_client_ids(world["user1"], db_path=world["db_path"])
    assert ids == {world["client1"]}

    # Unrestricted only for an admin; an ownerless viewer gets an empty set.
    assert access.owned_client_ids(world["admin"], db_path=world["db_path"]) is None
    assert access.owned_client_ids(None, db_path=world["db_path"]) == set()


def test_filter_rows_by_owner_scopes_the_firm_wide_queues(world):
    """Action Center / filing-calendar rows carry a client_id, so one call
    scopes the whole list — and the counts derived from it."""
    rows = [
        {"item_ref": "a", "client_id": world["client1"]},
        {"item_ref": "b", "client_id": world["client2"]},
        {"item_ref": "c", "client_id": world["client_admin"]},
    ]

    assert [
        r["item_ref"]
        for r in access.filter_rows_by_owner(world["user1"], rows, db_path=world["db_path"])
    ] == ["a"]
    assert [
        r["item_ref"]
        for r in access.filter_rows_by_owner(world["user2"], rows, db_path=world["db_path"])
    ] == ["b"]
    assert len(access.filter_rows_by_owner(world["admin"], rows, db_path=world["db_path"])) == 3
    assert access.filter_rows_by_owner(None, rows, db_path=world["db_path"]) == []


def test_visibility_helpers_scoping_the_run_counts(world):
    """The filesystem/folder view of a client is still keyed by NAME, so the
    name set has to be exact — and empty for an ownerless viewer."""
    assert access.visible_client_names(world["user1"], db_path=world["db_path"]) == {"Test Client"}
    assert access.visible_client_names(world["user2"], db_path=world["db_path"]) == {
        "Meridian Fabrics"
    }
    assert access.visible_client_names(world["admin"], db_path=world["db_path"]) is None
    assert access.visible_client_names(None, db_path=world["db_path"]) == set()


def test_owner_argument_is_required_so_a_forgotten_scope_cannot_compile():
    """The viewer is mandatory on every scoped read — a call site that forgot
    it raises TypeError instead of silently returning everything."""
    import inspect

    for func in (
        clients.list_clients,
        clients.get_client,
        clients.find_client,
        documents.list_documents,
        documents.get_document,
    ):
        params = inspect.signature(func).parameters
        assert any(
            p.kind is p.KEYWORD_ONLY and p.default is p.empty
            for p in params.values()
        ), f"{func.__name__} accepts a missing viewer"

    create_params = inspect.signature(clients.create_client).parameters
    assert create_params["owner_user_id"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Id-keyed operations: the id arrives from the client, so ownership is checked
# before anything is read OR written
# ---------------------------------------------------------------------------


@pytest.fixture()
def docs(world):
    """One document for each user's client, uploaded by its owner."""
    mine = documents.upload_document(
        client_id=world["client1"], filename="GSTR-2B_Aug.xlsx", file_bytes=b"a,b\n1,2\n",
        period="2026-08", actor="User1", viewer=world["user1"], db_path=world["db_path"],
    )
    theirs = documents.upload_document(
        client_id=world["client2"], filename="Tally_Aug.xlsx", file_bytes=b"x,y\n1,2\n",
        period="2026-08", actor="User2", viewer=world["user2"], db_path=world["db_path"],
    )
    return {**world, "mine": mine["document_id"], "theirs": theirs["document_id"]}


def test_upload_into_another_users_client_is_refused(docs):
    """The document's owner is derived from the target client, so an
    unchecked client_id would let a user file into somebody else's client."""
    with pytest.raises(documents.DocumentAccessDenied):
        documents.upload_document(
            client_id=docs["client2"], filename="Injected.xlsx", file_bytes=b"a,b\n",
            period="2026-08", actor="User1", viewer=docs["user1"], db_path=docs["db_path"],
        )

    # ...and nothing new landed on that client.
    theirs = documents.list_documents(
        viewer=docs["user2"], client_id=docs["client2"], db_path=docs["db_path"]
    )
    assert len(theirs) == 1


def test_document_mutations_on_another_users_document_are_refused(docs):
    db = docs["db_path"]
    viewer = docs["user1"]
    doc_id = docs["theirs"]

    with pytest.raises(documents.DocumentAccessDenied):
        documents.soft_delete_document(doc_id, actor="User1", viewer=viewer, db_path=db)
    with pytest.raises(documents.DocumentAccessDenied):
        documents.restore_document(doc_id, actor="User1", viewer=viewer, db_path=db)
    with pytest.raises(documents.DocumentAccessDenied):
        documents.discard_document(doc_id, actor="User1", viewer=viewer, db_path=db)
    with pytest.raises(documents.DocumentAccessDenied):
        documents.set_document_notes(doc_id, "tampered", viewer=viewer, db_path=db)
    with pytest.raises(documents.DocumentAccessDenied):
        documents.add_version(
            doc_id, filename="x.xlsx", file_bytes=b"a,b\n", actor="User1", viewer=viewer, db_path=db
        )
    with pytest.raises(documents.DocumentAccessDenied):
        documents.used_in(doc_id, viewer=viewer, db_path=db)
    with pytest.raises(documents.DocumentAccessDenied):
        documents.list_edit_log(doc_id, viewer=viewer, db_path=db)
    with pytest.raises(documents.DocumentAccessDenied):
        documents.get_current_version(doc_id, viewer=viewer, db_path=db)

    # The owner's document is untouched and still visible to them.
    theirs = documents.list_documents(
        viewer=docs["user2"], client_id=docs["client2"], db_path=db
    )
    assert len(theirs) == 1 and theirs[0]["is_deleted"] == 0


def test_version_bytes_are_refused_for_another_users_document(docs):
    """The stored file bytes are the most sensitive part of a document."""
    conn = sqlite3.connect(docs["db_path"])
    version_id = conn.execute(
        "SELECT current_version_id FROM documents WHERE document_id = ?", (docs["theirs"],)
    ).fetchone()[0]
    conn.close()

    with pytest.raises(documents.DocumentAccessDenied):
        documents.get_version_bytes(version_id, viewer=docs["user1"], db_path=docs["db_path"])

    assert documents.get_version_bytes(
        version_id, viewer=docs["user2"], db_path=docs["db_path"]
    ) is not None


def test_resolving_another_users_queue_entry_is_refused(docs):
    """A queue entry reclassifies a real document, so it needs the same gate."""
    queued = documents.upload_document(
        client_id=docs["client2"], filename="mystery_document.pdf", file_bytes=b"%PDF-1.4",
        period=None, actor="User2", viewer=docs["user2"], db_path=docs["db_path"],
    )
    conn = sqlite3.connect(docs["db_path"])
    entry_id = conn.execute(
        "SELECT entry_id FROM unified_review_queue_entries WHERE document_id = ?",
        (queued["document_id"],),
    ).fetchone()[0]
    conn.close()

    with pytest.raises(documents.DocumentAccessDenied):
        documents.resolve_queue_entry(
            entry_id, resolved_doc_type="GSTR-2B", notes=None, actor="User1",
            viewer=docs["user1"], db_path=docs["db_path"],
        )

    # Still pending, and the owner can resolve it.
    documents.resolve_queue_entry(
        entry_id, resolved_doc_type="GSTR-2B", notes=None, actor="User2",
        viewer=docs["user2"], db_path=docs["db_path"],
    )
    assert documents.list_queue("pending", viewer=docs["user2"], db_path=docs["db_path"]) == []


def test_reassign_refuses_both_ends_and_moves_the_owner(docs):
    """A reassignment must neither move somebody else's document nor push a
    document into somebody else's client — and the owner follows the client."""
    db = docs["db_path"]

    with pytest.raises(documents.DocumentAccessDenied):
        documents.reassign_document(
            docs["theirs"], docs["client1"], reason="mine now", actor="User1",
            viewer=docs["user1"], db_path=db,
        )
    with pytest.raises(documents.DocumentAccessDenied):
        documents.reassign_document(
            docs["mine"], docs["client_admin"], reason="not my client", actor="User1",
            viewer=docs["user1"], db_path=db,
        )

    # A legitimate reassignment by the admin moves the owner with the client.
    documents.reassign_document(
        docs["mine"], docs["client2"], reason="filed against the wrong entity",
        actor="admin", viewer=docs["admin"], db_path=db,
    )
    row = documents.get_document(docs["mine"], viewer=docs["admin"], db_path=db)
    assert row["client_id"] == docs["client2"]
    assert row["owner_user_id"] == docs["user2"]["user_id"]

    # The old owner loses it; the new one gains it.
    assert documents.find_document(docs["mine"], viewer=docs["user1"], db_path=db) is None
    assert documents.find_document(docs["mine"], viewer=docs["user2"], db_path=db) is not None


# ---------------------------------------------------------------------------
# The Action Center's Report tab
# ---------------------------------------------------------------------------


def test_report_periods_are_scoped(world):
    from src.action_center import report as report_svc

    db = world["db_path"]
    assert report_svc.list_report_periods(world["user1"], db_path=db) == []
    assert report_svc.list_report_periods(world["admin"], db_path=db) == []
    assert report_svc.list_report_runs(viewer=world["user1"], db_path=db) == []


def test_building_a_report_for_another_users_client_is_refused(world):
    from src.action_center import report as report_svc

    with pytest.raises(access.AccessDenied):
        report_svc.build_period_report(
            world["client2"], "2026-08", viewer=world["user1"], db_path=world["db_path"]
        )
    # The admin is allowed through to the "no run yet" answer instead.
    with pytest.raises(report_svc.ReportError):
        report_svc.build_period_report(
            world["client2"], "2026-08", viewer=world["admin"], db_path=world["db_path"]
        )


def test_report_periods_scope_a_real_run_to_its_owner(world):
    """A run exists for User2's client: User2 and the Admin see it, User1
    does not. (The rows are inserted directly — this test is about the scope
    filter, not about executing the engine.)"""
    from src.action_center import report as report_svc

    db = world["db_path"]
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO runs
            (client_id, client_name_at_run, period, recon_type, run_timestamp, config_snapshot)
        VALUES (?, 'Meridian Fabrics', '2026-08', 'GST', '2026-09-01T00:00:00+00:00', '{}')
        """,
        (world["client2"],),
    )
    conn.commit()
    conn.close()

    def labels(viewer):
        return sorted(p["label"] for p in report_svc.list_report_periods(viewer, db_path=db))

    assert labels(world["user2"]) == ["Meridian Fabrics · 2026-08"]
    assert labels(world["admin"]) == ["Meridian Fabrics · 2026-08"]
    assert labels(world["user1"]) == []
    assert labels(None) == []


# ---------------------------------------------------------------------------
# The same rule in the other module that store uploads
# ---------------------------------------------------------------------------


def test_upload_reads_fail_closed_without_a_viewer(docs):
    """No viewer is not "everything": these modules' lists return nothing and
    their id lookups return None rather than another user's file."""
    from src.invoice_extract import service as ie
    from src.ingestion_ai import service as ia

    db = docs["db_path"]

    assert ie.list_uploads(db_path=db) == []
    assert ie.list_export_batches(db_path=db) == []
    assert ie.list_review_queue(db_path=db) == []
    assert ie.review_queue_summary(db_path=db)["open_count"] == 0
    assert ie.get_upload(docs["theirs"], db_path=db) is None
    assert ie.get_upload_bytes(docs["theirs"], db_path=db) is None

    assert ia.list_uploads(db_path=db) == []
    assert ia.list_uploads_with_context(db_path=db) == []
    assert ia.get_upload(1, db_path=db) is None


def test_duplicate_filenames_are_scoped_to_the_viewer(docs):
    """A shared filename elsewhere in the vault is neither revealed nor allowed
    to badge this user's own document as a duplicate."""
    db = docs["db_path"]
    for _ in range(2):
        documents.upload_document(
            client_id=docs["client2"], filename="shared_name.xlsx", file_bytes=b"a,b\n",
            period="2026-08", actor="User2", viewer=docs["user2"], db_path=db,
        )

    assert "shared_name.xlsx" in documents.find_duplicate_filenames(
        viewer=docs["user2"], db_path=db
    )
    assert documents.find_duplicate_filenames(viewer=docs["user1"], db_path=db) == set()
    assert documents.find_duplicate_filenames(viewer=docs["admin"], db_path=db) == {
        "shared_name.xlsx"
    }


def test_every_scoped_call_in_the_app_passes_a_viewer():
    """A structural guard: scan the codebase for calls to the scoped services
    and fail if any omits the viewer. This is what keeps a future call site
    from quietly reading across the silo.

    Matched by RECEIVER + name, because several modules have same-named
    helpers (``ddb.get_document(conn, id)`` is the unscoped data layer, not
    ``documents.get_document(id, viewer=...)``).
    """
    import ast
    from pathlib import Path

    # receiver -> the function names on it that must be given a viewer
    SCOPED: dict[str, set[str]] = {
        "clients": {
            "get_client", "find_client", "list_clients", "list_contacts", "list_coa",
            "list_snapshots", "list_snapshot_years", "list_edit_log",
            "update_client_field", "update_client_details", "set_client_active",
            "add_contact", "remove_contact", "update_contact_field", "bulk_import_coa",
            "add_coa_row", "delete_coa_row", "bulk_import_snapshot", "add_snapshot_row",
            "delete_snapshot_row",
        },
        "documents": {
            "list_documents", "get_document", "find_document", "list_versions",
            "get_current_version", "get_version_bytes", "set_document_notes", "used_in",
            "documents_for_target", "discard_document", "resolve_queue_entry",
            "soft_delete_document", "restore_document", "reassign_document",
            "upload_document", "add_version", "list_queue", "queue_progress",
            "list_recently_deleted", "link_evidence", "find_duplicate_filenames",
            "list_edit_log",
        },
        "report_svc": {"list_report_periods", "list_report_runs", "build_period_report"},
        "ie": {
            "list_uploads", "get_upload", "get_upload_bytes", "list_review_queue",
            "list_export_batches", "review_queue_summary", "regenerate_export_bytes",
            "generate_export",
        },
        "ia": {
            "list_uploads", "list_uploads_with_context", "get_upload",
            "suggest_period_for_upload", "set_upload_period", "re_run_mapping",
        },
        "discovery": {"list_clients"},
    }
    # These declare the viewer as their FIRST parameter, so a positional call is fine.
    POSITIONAL_VIEWER = {"discovery": {"list_clients"}, "report_svc": {"list_report_periods"}}

    offenders: list[str] = []
    for folder in ("setu", "src"):
        for path in Path(REPO_ROOT, folder).rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                    continue
                receiver, name = func.value.id, func.attr
                if name not in SCOPED.get(receiver, set()):
                    continue
                if any(k.arg == "viewer" for k in node.keywords):
                    continue
                if node.args and name in POSITIONAL_VIEWER.get(receiver, set()):
                    continue
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {receiver}.{name}()")

    assert offenders == [], "scoped call(s) missing a viewer: " + ", ".join(offenders)
