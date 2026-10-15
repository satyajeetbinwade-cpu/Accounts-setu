"""Server-side access control — who may see which client's data.

Every client-scoped read funnels through here. Two access roles exist:

``admin``  firm-wide visibility, plus user management.
``user``   sees ONLY the clients it owns (``end_clients.owner_user_id``).

The access role is a column on ``users`` (``role``), projected onto every
user dict by ``src.auth.service``, so a viewer dict is all a query needs in
order to be scoped. This module is deliberately free of any ``src.clients``
import at module level (the client service imports *this* one, and the
dependency is closed lazily inside the functions below).

``SYSTEM_VIEWER`` is for trusted, non-request code paths — migrations,
seeding, and the reconciliation engine acting on a client the caller has
already been told is authorised. It is an admin view, and spelling it out
at the call site is what makes an unintended bypass visible in review.
Unscoped reads are the exception, never the default.
"""

from __future__ import annotations

from typing import Any, Optional

ADMIN_ROLE = "admin"
USER_ROLE = "user"

# F1 role *names* that carry firm-wide visibility. Kept here so the access
# decisions live in one place rather than being re-derived per module.
ADMIN_ROLE_NAMES: tuple[str, ...] = ("Admin", "Partner")

# Trusted internal caller (migration, seed, engine). Admin-equivalent.
SYSTEM_VIEWER: dict[str, Any] = {
    "user_id": 0,
    "username": "system",
    "display_name": "System",
    "role": ADMIN_ROLE,
    "role_name": "Admin",
}


class AccessDenied(Exception):
    """A viewer asked for a client (or a record of one) that is not theirs.

    Surfaces to the UI as 403/404 — the caller decides which; the data layer
    only ever says "not yours".
    """


# ---------------------------------------------------------------------------
# Viewer projection
# ---------------------------------------------------------------------------


def viewer_role(viewer: Optional[dict[str, Any]]) -> str:
    """The access role of a viewer. An absent/unknown viewer is the LEAST
    privileged role, never admin — a missing viewer must not widen access."""
    if not viewer:
        return USER_ROLE
    explicit = str(viewer.get("role") or "").strip().lower()
    if explicit in (ADMIN_ROLE, USER_ROLE):
        return explicit
    if str(viewer.get("role_name") or "") in ADMIN_ROLE_NAMES:
        return ADMIN_ROLE
    if not viewer.get("user_id"):
        # A viewer with no id and no recognisable role can't own anything;
        # treating it as an admin is what would leak, so don't.
        return USER_ROLE
    return USER_ROLE


def is_admin(viewer: Optional[dict[str, Any]]) -> bool:
    return viewer_role(viewer) == ADMIN_ROLE


def viewer_user_id(viewer: Optional[dict[str, Any]]) -> int:
    try:
        return int((viewer or {}).get("user_id") or 0)
    except (TypeError, ValueError):
        return 0


def viewer_label(viewer: Optional[dict[str, Any]]) -> str:
    return str((viewer or {}).get("username") or "")


def owner_filter(viewer: Optional[dict[str, Any]]) -> Optional[int]:
    """The ``owner_user_id`` to filter a query by, or ``None`` when the
    viewer is unrestricted (admin) — the one value every scoped query in the
    app is built from."""
    return None if is_admin(viewer) else viewer_user_id(viewer)


# ---------------------------------------------------------------------------
# Client records
# ---------------------------------------------------------------------------


def can_access_client_row(viewer: Optional[dict[str, Any]], client: Optional[dict[str, Any]]) -> bool:
    """May this viewer see this ``end_clients`` row?"""
    if client is None:
        return False
    if is_admin(viewer):
        return True
    owner = client.get("owner_user_id")
    if owner is None:
        # Rows predating ownership belong to nobody the DB knows: admin-only,
        # so a standard user can never reach them by guessing an id.
        return False
    try:
        return int(owner) == viewer_user_id(viewer)
    except (TypeError, ValueError):
        return False


def require_client_row(viewer: Optional[dict[str, Any]], client: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Return the row, or raise :class:`AccessDenied`.

    ``None`` (no such client) is also denied, so a caller can't distinguish
    "belongs to someone else" from "does not exist" — the two are the same
    answer to a standard user, and differ only for an admin.
    """
    if can_access_client_row(viewer, client):
        assert client is not None
        return client
    raise AccessDenied("Client not found or not accessible.")


# ---------------------------------------------------------------------------
# Client *names* — the filesystem/runs view of a client (data/<name>/...)
# ---------------------------------------------------------------------------


def owned_client_names(viewer: Optional[dict[str, Any]], *, db_path=None) -> set[str]:
    """Names of the clients this viewer owns. Empty for an ownerless viewer."""
    uid = viewer_user_id(viewer)
    if not uid:
        return set()
    from src.clients import db as cdb
    from src import db as recon_db

    conn = recon_db.get_connection(db_path)
    try:
        return cdb.list_client_names_for_owner(conn, uid)
    finally:
        conn.close()


def owned_client_ids(viewer: Optional[dict[str, Any]], *, db_path=None) -> Optional[set[int]]:
    """Ids of the clients this viewer owns, or ``None`` when unrestricted.

    The id counterpart of :func:`visible_client_names`, for the module queues
    that key their rows by ``client_id`` (Action Center items, filing calendar
    entries) rather than by name.
    """
    if is_admin(viewer):
        return None
    uid = viewer_user_id(viewer)
    if not uid:
        return set()
    from src.clients import db as cdb
    from src import db as recon_db

    conn = recon_db.get_connection(db_path)
    try:
        return cdb.list_client_ids_for_owner(conn, uid)
    finally:
        conn.close()


def visible_client_names(viewer: Optional[dict[str, Any]], *, db_path=None) -> Optional[set[str]]:
    """Every client name the viewer may see, or ``None`` when unrestricted.

    Names with no owning row (a folder that predates ownership, or a run for
    a client whose row was deleted) are NOT included for a standard user:
    unknown ownership resolves to admin-only, so nothing leaks by omission.
    """
    if is_admin(viewer):
        return None
    return owned_client_names(viewer, db_path=db_path)


def all_client_record_names(*, db_path=None) -> set[str]:
    """The legal names of every client record on the platform.

    The admin-side counterpart of :func:`owned_client_names`: used to make a
    client that was just created selectable before any file has been uploaded
    for it (its ``data/<name>/`` folder only appears once something lands).
    """
    from src.clients import db as cdb
    from src import db as recon_db

    conn = recon_db.get_connection(db_path)
    try:
        return cdb.list_all_client_names(conn)
    finally:
        conn.close()


def can_access_client_name(viewer: Optional[dict[str, Any]], client_name: Optional[str], *, db_path=None) -> bool:
    if not client_name:
        return False
    visible = visible_client_names(viewer, db_path=db_path)
    if visible is None:
        return True
    return client_name in visible


def require_client_name(viewer: Optional[dict[str, Any]], client_name: Optional[str], *, db_path=None) -> str:
    """Return the client name, or raise :class:`AccessDenied`."""
    if can_access_client_name(viewer, client_name, db_path=db_path):
        assert client_name
        return client_name
    raise AccessDenied("Client not found or not accessible.")


def filter_rows_by_owner(
    viewer: Optional[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    key: str = "client_id",
    db_path=None,
) -> list[dict[str, Any]]:
    """Drop every row whose ``key`` is not one of the viewer's clients.

    For the queue-shaped reads that legitimately span clients (the Action
    Center's flagged items, the filing calendar): the module stores a
    ``client_id`` on each row, so one call scopes the whole list. An admin gets
    the list unchanged; an ownerless viewer gets nothing.
    """
    owned = owned_client_ids(viewer, db_path=db_path)
    if owned is None:
        return rows
    return [row for row in rows if row.get(key) in owned]


# ---------------------------------------------------------------------------
# Client ids — the primary key the release-fly lineage stores on every record
# ---------------------------------------------------------------------------


def can_access_client_id(viewer: Optional[dict[str, Any]], client_id: Optional[int], *, db_path=None) -> bool:
    """May this viewer see this ``end_clients.client_id``?

    An id that does not exist is denied for a standard user for the same
    reason as one owned by somebody else: it is simply not in their list, so
    the two answers are indistinguishable.
    """
    if client_id is None:
        return False
    owned = owned_client_ids(viewer, db_path=db_path)
    if owned is None:
        return True
    try:
        return int(client_id) in owned
    except (TypeError, ValueError):
        return False


def require_client_id(viewer: Optional[dict[str, Any]], client_id: Optional[int], *, db_path=None) -> int:
    """Return the id, or raise :class:`AccessDenied`."""
    if can_access_client_id(viewer, client_id, db_path=db_path):
        assert client_id is not None
        return int(client_id)
    raise AccessDenied("Client not found or not accessible.")
