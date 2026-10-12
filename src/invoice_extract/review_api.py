"""The read-side API the Invoice Extraction review screen calls.

Three things the screen needs and should not have to assemble itself:

* :func:`get_upload_progress` — one call for "where is this upload?" instead of
  three (the triage run, the extraction counts, the review counts).
* :func:`get_page_image` / :func:`get_page_info` — the page picture the viewer
  draws, ALWAYS upright, rendered once and cached on disk.
* :func:`list_aliases` / :func:`confirm_direction` — the alias side of the
  direction decision, with the individual's-name refusal returned as a REASON
  rather than thrown, so a screen can show it without a try/except.

Gated by ``invoice_extract.v2.enabled`` (default FALSE): everything here reads
v2 data. No screen code lives here.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Optional

from src import data_paths
from src.invoice_extract.service import InvoiceExtractError, _connect

#: The dpi the viewer asks for by default. Part of the cache key, so changing it
#: does not invalidate (or reuse) images rendered at another size.
DEFAULT_PAGE_DPI = 120

#: The biggest page number we will render. A 31-page bundle is the real case;
#: this only bounds a nonsense value.
MAX_PAGE = 5000

#: The page-image formats the cache holds.
IMAGE_FORMAT = "PNG"


def require_enabled(db_path=None) -> None:
    from src.invoice_extract import extract as extract_mod

    extract_mod.require_enabled(db_path=db_path)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _dict_row(cur: sqlite3.Cursor, row) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {name: row[i] for i, name in enumerate([d[0] for d in cur.description])}


def _page_number(page: Any) -> int:
    """A validated 1-based page number.

    ``int()`` on a string would happily accept "2" but must not accept ``"../2"``,
    a float, a bool, a path, or 0/negative — the page number reaches a cache
    filename and a renderer, so it is validated before either sees it.
    """
    if isinstance(page, bool) or page is None:
        raise InvoiceExtractError(f"{page!r} is not a page number.")
    try:
        number = int(str(page).strip())
    except (TypeError, ValueError):
        raise InvoiceExtractError(f"{page!r} is not a page number.") from None
    if number < 1 or number > MAX_PAGE:
        raise InvoiceExtractError(f"Page {page!r} is out of range (1-{MAX_PAGE}).")
    return number


def _load_upload(conn: sqlite3.Connection, upload_id: int) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM invoice_uploads WHERE upload_id = ?", (int(upload_id),))
    upload = _dict_row(cur, cur.fetchone())
    if upload is None:
        raise InvoiceExtractError(f"Unknown upload {upload_id}.")
    return upload


def _page_rows(upload_id: int, *, db_path=None) -> dict[int, dict[str, Any]]:
    """The triage run's per-page rows, keyed by page. {} when there is no run."""
    from src.invoice_extract import triage as triage_mod

    info = triage_mod.get_triage(upload_id, db_path=db_path)
    return {int(p["page"]): p for p in info.get("pages") or []}


def _file_bytes(conn: sqlite3.Connection, upload_id: int) -> bytes:
    row = conn.execute(
        "SELECT file_bytes FROM invoice_uploads WHERE upload_id = ?", (int(upload_id),),
    ).fetchone()
    if row is None or not row[0]:
        raise InvoiceExtractError(
            f"Upload {upload_id} has no stored file bytes, so no page can be rendered."
        )
    return bytes(row[0])


def _cache_key(upload_id: int, page: int, rotation: int, dpi: int, digest: str) -> str:
    """``<upload>_<page>_<rotation>_<dpi>_<content hash>``.

    Built ONLY from validated integers and a hex digest, so the key cannot carry
    a separator, a traversal or an absolute path. The content hash means a
    re-uploaded file at the same upload id re-renders instead of serving the old
    picture.
    """
    return f"{int(upload_id)}_{int(page)}_{int(rotation)}_{int(dpi)}_{digest[:16]}"


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


def get_upload_progress(upload_id: int, *, db_path=None) -> dict[str, Any]:
    """Everything the screen needs to draw a progress panel, in one call.

    * ``triage``  — the run's status and page counts, including how many pages
      the FALLBACK leg served (``fallback_pages``), so a silently failing
      primary is visible.
    * ``extraction`` — per-document status counts.
    * ``review``  — how many documents are still to look at, how many have been
      signed off, how many still ask a question, and how many are excluded.

    Counting "still asks a question" builds the review model for each document
    that still needs one. That is a handful of indexed queries per document, and
    a signed-off document is skipped — but it is the one part of this call whose
    cost grows with the size of the upload.
    """
    require_enabled(db_path)
    from src.invoice_extract import extract as extract_mod
    from src.invoice_extract import rules as rules_mod
    from src.invoice_extract import triage as triage_mod
    from src.invoice_extract.doc_actions import EXTRACTABLE_CATEGORIES, is_reviewed

    triage_info = triage_mod.get_triage(upload_id, db_path=db_path)
    run = triage_info.get("run") or {}
    documents = triage_info.get("documents") or []

    counts = extract_mod.extraction_status_counts(upload_id, db_path=db_path)
    statuses = {
        name: int(counts.get(name, 0))
        for name in ("queued", "running", "complete", "partial", "failed", "skipped")
    }

    in_review = [
        d for d in documents
        if not d.get("excluded") and d.get("category") in EXTRACTABLE_CATEGORIES
    ]
    reviewed_ids: set[int] = set()
    needs_you = 0
    unreviewed = 0
    for document in in_review:
        document_id = int(document["document_id"])
        if is_reviewed(document_id, db_path=db_path):
            reviewed_ids.add(document_id)
            continue
        unreviewed += 1
        try:
            model = rules_mod.get_review_model(document_id, db_path=db_path)
        except InvoiceExtractError:
            # No extraction to review yet — still a document to look at.
            needs_you += 1
            continue
        if model.get("needs_you"):
            needs_you += 1

    return {
        "upload_id": int(upload_id),
        "triage": {
            "run_id": run.get("run_id"),
            "status": run.get("status"),
            "pages_total": int(run.get("pages_total") or 0),
            "pages_ok": int(run.get("pages_ok") or 0),
            "pages_failed": int(run.get("pages_failed") or 0),
            "fallback_pages": int(run.get("fallback_pages") or 0),
            "provider": run.get("provider"),
            "model": run.get("model"),
            "cost_usd": run.get("cost_usd"),
            "error": run.get("error"),
        },
        "extraction": {**statuses, "total": sum(statuses.values())},
        "review": {
            "documents": len(documents),
            "to_review": len(in_review),
            "reviewed": len(reviewed_ids),
            "with_needs_you": needs_you,
            "excluded": sum(1 for d in documents if d.get("excluded")),
            "unassigned_pages": len(triage_info.get("unassigned") or []),
        },
    }


# ---------------------------------------------------------------------------
# Page images
# ---------------------------------------------------------------------------


def get_page_info(upload_id: int, *, db_path=None) -> list[dict[str, Any]]:
    """Every page of the upload's source file, in order.

    ``width`` / ``height`` are the UPRIGHT page dimensions — the same way round
    as the image :func:`get_page_image` returns — in PDF POINTS for a PDF and in
    PIXELS for an image source. They describe the media box, not a rendering at
    any particular dpi, so the screen can lay out an overlay without rendering.
    """
    require_enabled(db_path)
    conn = _connect(db_path)
    try:
        upload = _load_upload(conn, upload_id)
        data = _file_bytes(conn, upload_id)
    finally:
        conn.close()

    pages = _page_rows(upload_id, db_path=db_path)
    info = _page_dimensions(data, upload.get("source_format") or "pdf")
    out: list[dict[str, Any]] = []
    for page, (width, height) in sorted(info.items()):
        row = pages.get(page) or {}
        rotation = int(row.get("rotation_deg") or 0) % 360
        if rotation in (90, 270):
            width, height = height, width
        out.append({
            "page": page,
            "rotation_deg": rotation,
            "width": width,
            "height": height,
            "duplicate_of_page": row.get("duplicate_of_page"),
        })
    return out


def _page_dimensions(data: bytes, source_format: str) -> dict[int, tuple[float, float]]:
    """{page: (width, height)} in raw page units — metadata only, no rendering."""
    if source_format != "pdf":
        try:
            from io import BytesIO

            from PIL import Image

            with Image.open(BytesIO(data)) as img:
                return {1: (float(img.size[0]), float(img.size[1]))}
        except Exception:  # noqa: BLE001 — an unreadable source is simply empty
            return {}
    try:
        import fitz

        doc = fitz.open(stream=data, filetype="pdf")
        try:
            return {
                i + 1: (float(page.rect.width), float(page.rect.height))
                for i, page in enumerate(doc)
            }
        finally:
            doc.close()
    except Exception:  # noqa: BLE001
        return {}


def get_page_image(upload_id: int, page: Any, *, dpi: int = DEFAULT_PAGE_DPI, db_path=None) -> bytes:
    """One page as PNG bytes, ALWAYS upright, rendered once and cached on disk.

    "Upright" means the triage run's ``rotation_deg`` for that page has been
    applied (a page the model found sideways is stored rotated, and a box in the
    DB is measured after that rotation — so the picture the viewer draws must be
    the rotated one, or every highlight would be wrong).

    The cache lives under the data root in a reserved, git-ignored folder, keyed
    by upload + page + rotation + dpi + the hash of the source bytes, so:
    a second call with the same arguments never re-renders; a different dpi or a
    re-uploaded file gets its own entry rather than a stale picture.

    ``page`` must be a plain 1-based integer — a string is coerced, anything
    that is not a number, or an out-of-range one, is refused before it can reach
    a filename or the renderer.
    """
    require_enabled(db_path)
    number = _page_number(page)
    dpi = int(dpi)
    if dpi < 24 or dpi > 600:
        raise InvoiceExtractError(f"dpi {dpi} is out of range (24-600).")

    conn = _connect(db_path)
    try:
        upload = _load_upload(conn, upload_id)
        data = _file_bytes(conn, upload_id)
    finally:
        conn.close()

    rotation = int((_page_rows(upload_id, db_path=db_path).get(number) or {}).get("rotation_deg") or 0) % 360
    digest = hashlib.sha256(data).hexdigest()
    path = data_paths.page_cache_path(
        _cache_key(int(upload_id), number, rotation, dpi, digest)
    )
    if path.exists():
        cached = path.read_bytes()
        if cached[:4] == b"\x89PNG":
            return cached
        path.unlink(missing_ok=True)          # a truncated cache entry: re-render

    from src.invoice_extract import crops as crops_mod

    source_format = str(upload.get("source_format") or "pdf")
    rendered = crops_mod.render_upright_page(
        data, source_format, number, dpi=dpi, rotation_deg=rotation,
    )
    from io import BytesIO

    buf = BytesIO()
    rendered.image.save(buf, format=IMAGE_FORMAT)
    png = buf.getvalue()

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(png)
        tmp.replace(path)                     # atomic: a reader never sees half a file
    except OSError:
        # A cache we cannot write is not a failure — the caller still gets the
        # image, it is simply recomputed next time.
        pass
    return png


def clear_page_cache(*, upload_id: Optional[int] = None) -> int:
    """Delete cached page images (all of them, or one upload's). Returns the count.

    Derived data only: nothing here is a source of truth, so a stale or corrupt
    cache is always safe to drop.
    """
    directory = data_paths.page_cache_dir()
    if not directory.is_dir():
        return 0
    prefix = f"{int(upload_id)}_" if upload_id is not None else None
    removed = 0
    for entry in directory.glob("*.png"):
        if prefix is not None and not entry.name.startswith(prefix):
            continue
        try:
            entry.unlink()
            removed += 1
        except OSError:  # pragma: no cover — a file we cannot remove is not fatal
            continue
    return removed


# ---------------------------------------------------------------------------
# Aliases + direction
# ---------------------------------------------------------------------------


def list_aliases(client_id: Optional[int] = None, *, db_path=None) -> list[dict[str, Any]]:
    """A client's remembered name aliases (active ones), for the screen."""
    from src.invoice_extract import rules as rules_mod

    return rules_mod.list_aliases(client_id, db_path=db_path)


def confirm_direction(
    document_id: int, direction: str, *, actor: str, remember_alias: bool = False,
    alias_text: Optional[str] = None, db_path=None,
) -> dict[str, Any]:
    """Settle a document's direction, optionally remembering the counterparty's
    name as an alias — AND RETURN THE REFUSAL AS A REASON rather than raising.

    A screen has to show "I cannot remember this name, and here is why"; an
    exception would be caught and flattened into a generic error. So a refusal
    comes back as ``{"ok": False, "reason": ...}`` and nothing is written.

    The rule itself is unchanged and still lives in the rules layer: an
    INDIVIDUAL's name is never stored as a client alias.
    """
    require_enabled(db_path)
    from src.invoice_extract import rules as rules_mod

    try:
        result = rules_mod.confirm_direction(
            document_id, direction, actor=actor,
            alias_text=alias_text, remember_alias=remember_alias, db_path=db_path,
        )
    except InvoiceExtractError as exc:
        return {"ok": False, "reason": str(exc), "document_id": int(document_id),
                "direction": direction, "alias": None}
    return {"ok": True, "reason": "", "alias": result.get("alias"), **result}


def alias_refusal_reason(
    document_id: int, alias_text: Optional[str] = None, *, db_path=None,
) -> Optional[str]:
    """Why this document's counterparty name cannot be remembered, or None.

    Exposed on its own so the screen can DISABLE the "remember" control and say
    why, instead of offering a button that fails.
    """
    from src.invoice_extract import direction as direction_mod
    from src.invoice_extract import rules as rules_mod

    context = rules_mod.build_context(document_id, db_path=db_path)
    text = alias_text or context.value("recipient_name")
    allowed, why = direction_mod.can_be_remembered_as_alias(context, text)
    return None if allowed else why


__all__ = [
    "DEFAULT_PAGE_DPI",
    "MAX_PAGE",
    "get_upload_progress",
    "get_page_info",
    "get_page_image",
    "clear_page_cache",
    "list_aliases",
    "confirm_direction",
    "alias_refusal_reason",
]
