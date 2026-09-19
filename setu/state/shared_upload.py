"""Shared upload / preview / viewer state for F3 (Document & Data Repository)
and F3-B (Invoice Extraction).

Both modules share the same upload-staging, per-file progress and in-app file
viewer behaviour. This single-inheritance base class holds the state + helpers
both consume, so the two screens can never drift apart on these behaviours
(the acceptance criterion "fixing one didn't leave the other looking or
behaving differently").

Subclasses are ``DocumentsState`` and ``InvoiceExtractState``; each keeps its
own module-specific fields and handlers on top of the shared loader.
"""

from __future__ import annotations

import base64
import os
import tempfile
from dataclasses import dataclass

import reflex as rx

from setu.state.auth_state import AuthState

# Per-file upload lifecycle. Every state change must be visibly distinct, not
# a colour swap on one badge — so the chip renders a different icon + label
# per stage rather than recolouring a single pill.
STAGE_QUEUED = "queued"
STAGE_UPLOADING = "uploading"
STAGE_PROCESSING = "processing"
STAGE_DONE = "done"
STAGE_NEEDS_REVIEW = "needs_review"
STAGE_FAILED = "failed"

# Viewer render kinds (what the file viewer can render inline).
VIEWER_IMAGE = "image"
VIEWER_PDF = "pdf"
VIEWER_TABLE = "table"     # xlsx / csv → first-sheet table
VIEWER_TEXT = "text"       # docx / csv / other text
VIEWER_DOWNLOAD = "download"  # formats that can't render inline

# Staged uploads are written to disk (per-session temp dir) so they survive a
# Reflex hot reload — a module-level dict would be wiped (documented trap).
_STAGE_DIR = os.path.join(tempfile.gettempdir(), "setu_upload_stage")


@dataclass
class PendingFile:
    """One file staged in the drop zone, pre-upload."""

    key: str
    filename: str
    size: int
    thumb_kind: str          # "image" | "pdf" | "icon"
    thumb_src: str           # data URI thumbnail (image / pdf first page) or ""
    ext: str
    # Reflex exposes dataclass FIELDS as state vars — a method can't be called
    # on a Var, so the human-readable size is materialised at construction.
    size_label: str = ""
    stage_path: str = ""     # absolute path to the staged bytes on disk


def _human_size(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


@dataclass
class UploadProgressRow:
    """Per-file upload progress (Queued → Uploading → Processing → Done…)."""

    key: str
    filename: str
    stage: str
    pct: int               # 0-100, used by Uploading
    label: str             # stage label / error text


def _read_staged_bytes(stage_path: str) -> bytes:
    if stage_path and os.path.exists(stage_path):
        with open(stage_path, "rb") as fh:
            return fh.read()
    return b""


def _discard_staged(*paths: str) -> None:
    for p in paths:
        if p:
            try:
                os.remove(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Byte helpers reused by both modules (thumbnail + viewer sniffing).
# ---------------------------------------------------------------------------


def _file_ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _image_thumb(file_bytes: bytes, *, max_dim: int = 160) -> str | None:
    try:
        from PIL import Image

        from io import BytesIO

        with Image.open(BytesIO(file_bytes)) as img:
            img.thumbnail((max_dim, max_dim))
            out = BytesIO()
            fmt = img.format or "PNG"
            if fmt.upper() == "JPEG":
                img = img.convert("RGB")
            img.save(out, format=fmt.upper() if fmt else "PNG")
        ext = "jpg" if out.getvalue()[:2] == b"\xff\xd8" else "png"
        mime = "image/jpeg" if ext == "jpg" else "image/png"
        return f"data:{mime};base64,{base64.b64encode(out.getvalue()).decode('ascii')}"
    except Exception:  # noqa: BLE001
        return None


def _pdf_first_page_png(file_bytes: bytes, *, dpi: int = 96) -> bytes | None:
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            if doc.page_count <= 0:
                return None
            pix = doc[0].get_pixmap(dpi=dpi)
            return pix.tobytes("png")
        finally:
            doc.close()
    except Exception:  # noqa: BLE001
        return None


def pdf_page_pngs(file_bytes: bytes, *, dpi: int = 120) -> list[str]:
    """Rasterise every page of a PDF to a base64 data-URI PNG list (for the
    in-app viewer's page controls). Returns [] if unavailable."""
    import base64 as _b64

    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception:  # noqa: BLE001
        return []
    pages: list[str] = []
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            pages.append(f"data:image/png;base64,{_b64.b64encode(pix.tobytes('png')).decode('ascii')}")
    except Exception:  # noqa: BLE001
        return pages
    finally:
        doc.close()
    return pages


def sniff_file(filename: str, file_bytes: bytes) -> dict:
    """Return ``{"kind": ..., ...}`` describing how the shared viewer can
    render a file's content, plus the data needed to do so (data URIs / table
    rows). Callers store only the lightweight parts in state and build the
    heavy render lazily."""
    ext = _file_ext(filename)
    if ext in ("jpg", "jpeg", "png"):
        return {"kind": VIEWER_IMAGE, "mime": "image/jpeg" if ext == "jpg" else "image/png"}
    if ext == "pdf":
        return {"kind": VIEWER_PDF, "page_count": _pdf_page_count(file_bytes)}
    if ext in ("xlsx", "xls"):
        return {"kind": VIEWER_TABLE}
    if ext == "csv":
        return {"kind": VIEWER_TABLE}
    if ext == "docx":
        return {"kind": VIEWER_TEXT}
    return {"kind": VIEWER_DOWNLOAD}


def _pdf_page_count(file_bytes: bytes) -> int:
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            return doc.page_count
        finally:
            doc.close()
    except Exception:  # noqa: BLE001
        return 0


class SharedUploadState(AuthState):
    """Upload staging + progress + in-app viewer state shared by F3 and F3-B.

    Subclasses own their module's final-commit handler; this base only holds
    the pre-upload staging and the viewer (which reads already-stored bytes
    via each module's own service).
    """

    # --- drop-zone staging (pre-upload) ----------------------------------
    pending: list[PendingFile] = []
    upload_progress: list[UploadProgressRow] = []

    # --- viewer (in-app file content) ------------------------------------
    viewer_open: bool = False
    viewer_title: str = ""
    viewer_kind: str = ""
    viewer_mime: str = ""
    viewer_image_src: str = ""        # single image / current PDF page
    viewer_pdf_pages: list[str] = []
    viewer_page: int = 1
    viewer_page_count: int = 0
    viewer_table_headers: list[str] = []
    viewer_table_rows: list[list[str]] = []
    viewer_text: str = ""
    viewer_download_name: str = ""
    viewer_download_b64: str = ""
    viewer_download_mime: str = ""
    # Zoom for the image/PDF viewer. Panning is native scroll on the
    # overflow container once zoomed — no custom drag JS needed.
    viewer_zoom: float = 1.0

    # ------------------------------------------------------------------
    # Staging
    # ------------------------------------------------------------------
    @rx.event
    async def stage(self, files: list[rx.UploadFile]):
        """Stage dropped files, building a thumbnail / type icon for each
        (the FilePreviewChip). Reads bytes via ``f.read()`` (a coroutine)."""
        for f in files:
            name = f.filename or "upload"
            data = await f.read()
            self._add_pending(name, data)

    def _add_pending(self, filename: str, data: bytes) -> None:
        ext = _file_ext(filename)
        thumb_kind = "icon"
        thumb_src = ""
        if ext in ("jpg", "jpeg", "png"):
            t = _image_thumb(data)
            if t:
                thumb_kind, thumb_src = "image", t
        elif ext == "pdf":
            png = _pdf_first_page_png(data)
            if png:
                thumb_kind, thumb_src = "pdf", f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"
        key = f"{filename}:{len(data)}"
        if any(p.key == key for p in self.pending):
            return
        # Persist bytes to disk so they survive hot reload + are cheaply
        # re-readable at upload time (thumbnails only live in the chip).
        os.makedirs(_STAGE_DIR, exist_ok=True)
        stage_path = os.path.join(_STAGE_DIR, f"{abs(hash(key)):x}.{ext or 'bin'}")
        with open(stage_path, "wb") as fh:
            fh.write(data)
        self.pending = self.pending + [
            PendingFile(key=key, filename=filename, size=len(data), thumb_kind=thumb_kind, thumb_src=thumb_src, ext=ext, size_label=_human_size(len(data)), stage_path=stage_path)
        ]

    def _pending_bytes(self, key: str) -> bytes:
        for p in self.pending:
            if p.key == key:
                return _read_staged_bytes(p.stage_path)
        return b""

    def _set_stage(self, key: str, stage: str, *, pct: int = 0, label: str = "") -> None:
        rows = []
        for r in self.upload_progress:
            if r.key == key:
                rows.append(UploadProgressRow(key=key, filename=r.filename, stage=stage, pct=pct, label=label))
            else:
                rows.append(r)
        self.upload_progress = rows

    @rx.event
    def begin_upload(self, key: str, filename: str, size: int):
        """Move a staged file into the progress list as Queued, then callers
        advance it through Uploading/Processing/Done in their own handler."""
        if any(r.key == key for r in self.upload_progress):
            return
        self.upload_progress = self.upload_progress + [
            UploadProgressRow(key=key, filename=filename, stage=STAGE_QUEUED, pct=0, label="Queued")
        ]

    @rx.event
    def remove_pending(self, key: str):
        for p in self.pending:
            if p.key == key:
                _discard_staged(p.stage_path)
        self.pending = [p for p in self.pending if p.key != key]

    @rx.event
    def clear_pending(self):
        for p in self.pending:
            _discard_staged(p.stage_path)
        self.pending = []

    # ------------------------------------------------------------------
    # Viewer
    # ------------------------------------------------------------------
    @rx.event
    def open_viewer(self, title: str, kind: str, mime: str):
        self.viewer_open = True
        self.viewer_title = title
        self.viewer_kind = kind
        self.viewer_mime = mime
        self.viewer_page = 1
        self.viewer_zoom = 1.0

    @rx.event
    def close_viewer(self):
        self.viewer_open = False
        self.viewer_kind = ""
        self.viewer_title = ""
        self.viewer_zoom = 1.0

    @rx.event
    def viewer_zoom_in(self):
        self.viewer_zoom = min(3.0, round(self.viewer_zoom + 0.25, 2))

    @rx.event
    def viewer_zoom_out(self):
        self.viewer_zoom = max(0.5, round(self.viewer_zoom - 0.25, 2))

    @rx.event
    def viewer_zoom_reset(self):
        self.viewer_zoom = 1.0

    @rx.event
    def viewer_page_next(self):
        if self.viewer_page < self.viewer_page_count:
            self.viewer_page += 1
            self._show_page()

    @rx.event
    def viewer_page_prev(self):
        if self.viewer_page > 1:
            self.viewer_page -= 1
            self._show_page()

    def _show_page(self) -> None:
        idx = self.viewer_page - 1
        if 0 <= idx < len(self.viewer_pdf_pages):
            self.viewer_image_src = self.viewer_pdf_pages[idx]


def _to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0