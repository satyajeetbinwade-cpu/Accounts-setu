"""Phase-2 crop geometry: region → crop image, and crop box → page coordinates.

TWO RESPONSIBILITIES, ONE OF EACH
--------------------------------
1. :func:`render_region_crops` turns a document's regions into crop PNGs. The
   page is rendered at the configured dpi, ROTATED UPRIGHT (`rotation_deg`
   clockwise, the same convention Phase 1 stores), the region box is cropped
   with padding, and the long edge is capped.
2. :func:`crop_box_to_page` is the ONE function that converts a box the model
   gave relative to a CROP back into page-normalised coordinates. Everything
   that needs that conversion calls this — the prompt's contract, the
   validation, the persistence and the scorer cannot drift apart.

WHY ROTATE BEFORE CROPPING
--------------------------
Phase 1's ``document_regions`` boxes are measured on the UPRIGHT page (the
prompt tells the model to return boxes "on the UPRIGHT page" and to report the
clockwise rotation needed to get there). So the crop must be taken from the
rotated image, and every box the Phase-2 model returns is relative to that
rotated crop. :func:`unrotate_box` maps a value's box back onto the RAW page so
the PDF text layer (which is always in raw page coordinates) can be searched.

PADDING
-------
``padding_frac`` (default 3%) is applied to the SHORTER side of the REGION, so
a short single-line region gets proportionally more breathing room than a tall
one, and a wide region is not padded horizontally beyond what it needs. The
padded box is then clamped to the page, so a region at the page edge simply
gets less padding rather than a black band.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any, Optional

DEFAULT_PADDING_FRAC = 0.03

#: How far outside its own crop a model box may stray (fraction of the crop)
#: before we treat it as unusable. Also the tolerance used when deciding
#: whether a box still lies inside one of the document's regions.
BOX_TOLERANCE = 0.02


@dataclass(frozen=True)
class PageRender:
    """One upright page, as raw pixels, ready to crop."""

    page: int
    image: Any          # PIL.Image (RGB), upright
    width: int
    height: int
    rotation_deg: int
    text_layer_chars: int


@dataclass(frozen=True)
class Crop:
    """One region's crop."""

    region_index: int          # 1-based, the order the model sees ("Region 1 = page 10")
    region_page: int           # the source page number
    png: bytes
    px_w: int
    px_h: int
    #: The crop's own box ON THE UPRIGHT PAGE, normalised 0-1 — the origin the
    #: model's crop-relative boxes are measured from.
    page_box: tuple[float, float, float, float]

    @property
    def label(self) -> str:
        return f"Region {self.region_index} = page {self.region_page}"


class CropError(Exception):
    """A region could not be rendered. The document is marked failed — never
    silently empty."""


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def clamp_box(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Clamp a normalised box into the page, keeping a positive size."""
    x, y, w, h = box
    x0, y0 = _clamp01(x), _clamp01(y)
    x1, y1 = _clamp01(x + w), _clamp01(y + h)
    if x1 <= x0:
        x1 = min(1.0, x0 + 1e-6)
    if y1 <= y0:
        y1 = min(1.0, y0 + 1e-6)
    return (x0, y0, x1 - x0, y1 - y0)


def padded_box(
    box: tuple[float, float, float, float], *, padding_frac: float = DEFAULT_PADDING_FRAC,
) -> tuple[float, float, float, float]:
    """Grow ``box`` by ``padding_frac`` of its SHORTER side, clamped to the page."""
    x, y, w, h = clamp_box(box)
    pad = max(0.0, float(padding_frac)) * min(w, h)
    return clamp_box((x - pad, y - pad, w + 2 * pad, h + 2 * pad))


def _upright_image(img: Any, rotation_deg: int) -> Any:
    """Rotate a page image CLOCKWISE by ``rotation_deg`` (0/90/180/270)."""
    deg = int(rotation_deg or 0) % 360
    if deg == 0:
        return img
    # PIL rotates counter-clockwise for positive angles; we need clockwise.
    return img.rotate(-deg, expand=True)


def render_upright_page(
    file_bytes: bytes, source_format: str, page: int, *, dpi: int, rotation_deg: int = 0,
) -> PageRender:
    """Render one page at ``dpi`` and rotate it upright. Raises CropError."""
    from PIL import Image

    if source_format == "pdf":
        import fitz

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            if page < 1 or page > doc.page_count:
                raise CropError(f"page {page} is outside the document (1-{doc.page_count})")
            fitz_page = doc.load_page(page - 1)
            text = fitz_page.get_text("text") or ""
            pix = fitz_page.get_pixmap(dpi=dpi)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        except CropError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CropError(f"could not render page {page}: {exc}") from exc
        finally:
            doc.close()
    else:
        try:
            img = Image.open(BytesIO(file_bytes)).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            raise CropError(f"could not open the image: {exc}") from exc
        text = ""

    img = _upright_image(img, rotation_deg)
    return PageRender(
        page=page, image=img, width=img.size[0], height=img.size[1],
        rotation_deg=int(rotation_deg or 0) % 360, text_layer_chars=len(text),
    )


def crop_region(
    rendered: PageRender,
    box: tuple[float, float, float, float],
    *,
    region_index: int,
    padding_frac: float = DEFAULT_PADDING_FRAC,
    max_edge_px: int,
) -> Crop:
    """Crop one region (with padding) out of an upright page render.

    The long edge of the result is capped at ``max_edge_px``; the aspect ratio
    is preserved. Raises CropError when the box is unusable — an empty or
    degenerate region must never be sent as a silently blank crop.
    """
    page_box = padded_box(box, padding_frac=padding_frac)
    x, y, w, h = page_box
    left = int(round(x * rendered.width))
    top = int(round(y * rendered.height))
    right = int(round((x + w) * rendered.width))
    bottom = int(round((y + h) * rendered.height))
    # At least one pixel each way — a 1px sliver is still a render, and the
    # caller's box came from a validated Phase-1 region.
    right = max(right, left + 1)
    bottom = max(bottom, top + 1)
    try:
        crop = rendered.image.crop((left, top, right, bottom))
    except Exception as exc:  # noqa: BLE001
        raise CropError(f"could not crop the region at {page_box}: {exc}") from exc
    if crop.size[0] < 1 or crop.size[1] < 1:
        raise CropError(f"the region at {page_box} is empty")

    long_edge = max(crop.size)
    cap = max(1, int(max_edge_px))
    if long_edge > cap:
        scale = cap / long_edge
        crop = crop.resize(
            (max(1, int(crop.size[0] * scale)), max(1, int(crop.size[1] * scale)))
        )

    buf = BytesIO()
    crop.save(buf, format="PNG")
    return Crop(
        region_index=region_index, region_page=rendered.page, png=buf.getvalue(),
        px_w=crop.size[0], px_h=crop.size[1], page_box=page_box,
    )


def render_region_crops(
    file_bytes: bytes,
    source_format: str,
    regions: list[dict[str, Any]],
    rotations: dict[int, int],
    *,
    dpi: int,
    max_edge_px: int,
    padding_frac: float = DEFAULT_PADDING_FRAC,
) -> tuple[list[Crop], dict[int, PageRender]]:
    """Crop every region of a document, in ``order_in_doc`` order.

    Returns ``(crops, renders_by_page)`` — the renders are handed back so the
    text-layer search does not have to re-render (and re-rotate) the page.
    Raises CropError for the first region that cannot be rendered, so the
    document is marked failed rather than sent with a hole in it.
    """
    renders: dict[int, PageRender] = {}
    crops: list[Crop] = []
    ordered = sorted(regions, key=lambda r: int(r.get("order_in_doc") or 0))
    for index, region in enumerate(ordered, start=1):
        page = int(region.get("page") or 0)
        if page < 1:
            raise CropError(f"region {index} has no usable page number")
        if page not in renders:
            renders[page] = render_upright_page(
                file_bytes, source_format, page,
                dpi=dpi, rotation_deg=int(rotations.get(page, 0) or 0),
            )
        box = (
            float(region.get("x") or 0.0), float(region.get("y") or 0.0),
            float(region.get("w") or 0.0), float(region.get("h") or 0.0),
        )
        if box[2] <= 0 or box[3] <= 0:
            raise CropError(f"region {index} on page {page} has no area")
        crops.append(crop_region(
            renders[page], box, region_index=index,
            padding_frac=padding_frac, max_edge_px=max_edge_px,
        ))
    if not crops:
        raise CropError("the document has no regions to read")
    return crops, renders


# ---------------------------------------------------------------------------
# THE coordinate conversion (crop-relative → page-normalised)
# ---------------------------------------------------------------------------


def parse_crop_box(raw: Any) -> Optional[tuple[float, float, float, float]]:
    """A model box relative to its crop, as fractions of the CROP (0-1).

    Accepts ``{"x","y","w","h"}``, corner forms (``x0/y0/x1/y1``,
    ``left/top/right/bottom``) and ``[x0,y0,x1,y1]`` / ``[x,y,w,h]`` lists.
    A value slightly outside the crop (within :data:`BOX_TOLERANCE`) is kept and
    clamped by the caller; a value wildly outside is treated as unusable so it
    cannot be silently folded onto the page.
    """
    if raw is None:
        return None
    x = y = w = h = None
    if isinstance(raw, dict):
        lowered = {str(k).lower(): v for k, v in raw.items()}

        def _num(*names: str) -> Optional[float]:
            for name in names:
                if lowered.get(name) is not None:
                    try:
                        return float(lowered[name])
                    except (TypeError, ValueError):
                        return None
            return None

        x = _num("x", "left", "x0")
        y = _num("y", "top", "y0")
        w = _num("w", "width")
        h = _num("h", "height")
        if w is None and h is None:
            x1 = _num("x1", "right")
            y1 = _num("y1", "bottom")
            if x is not None and x1 is not None:
                w = x1 - x
            if y is not None and y1 is not None:
                h = y1 - y
    elif isinstance(raw, (list, tuple)) and len(raw) >= 4:
        try:
            values = [float(v) for v in raw[:4]]
        except (TypeError, ValueError):
            return None
        if values[2] > values[0] and values[3] > values[1]:
            x, y, w, h = values[0], values[1], values[2] - values[0], values[3] - values[1]
        else:
            x, y, w, h = values
    if None in (x, y, w, h):
        return None
    assert x is not None and y is not None and w is not None and h is not None
    if w <= 0 or h <= 0:
        return None
    # A box may stray up to BOX_TOLERANCE (2%) outside its crop — model box
    # conventions are not pixel-exact. Beyond that it is a box the model read off
    # some other scale (0-1000, pixels), which must be refused rather than
    # silently folded onto the crop.
    if min(x, y) < -BOX_TOLERANCE or max(x + w, y + h) > 1.0 + BOX_TOLERANCE:
        return None
    return clamp_box((x, y, w, h))


def crop_box_to_page(
    box: tuple[float, float, float, float], crop: Crop,
) -> tuple[float, float, float, float]:
    """THE conversion: a crop-relative box → a page-normalised box.

    ``crop.page_box`` is where the crop sits on the UPRIGHT page (already
    padded), so this is a pure affine map: scale by the crop's size and offset
    by its origin. The result is clamped to the page.
    """
    cx, cy, cw, ch = crop.page_box
    x, y, w, h = box
    return clamp_box((cx + x * cw, cy + y * ch, w * cw, h * ch))


def box_px(crop: Crop, box: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """The pixel rectangle of a crop-relative box INSIDE its crop PNG."""
    x, y, w, h = box
    return (
        int(round(x * crop.px_w)), int(round(y * crop.px_h)),
        int(round((x + w) * crop.px_w)), int(round((y + h) * crop.px_h)),
    )


# ---------------------------------------------------------------------------
# Region membership + the inverse rotation (for the PDF text layer)
# ---------------------------------------------------------------------------


def boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw < bx or bx + bw < ax or ay + ah < by or by + bh < ay)


#: How much of a VALUE's own box an annotation must cover before the annotation
#: is treated as written ON that value rather than merely near it. Deliberately
#: generous: a handwritten figure scribbled over a printed amount shares only
#: part of its box with it, while a figure written in the next column shares
#: none.
HANDWRITING_OVERLAP_MIN = 0.3


def overlap_ratio_of(
    box: tuple[float, float, float, float], other: tuple[float, float, float, float],
) -> float:
    """The fraction of ``box``'s own area that ``other`` covers (0.0 - 1.0).

    Zero when they do not intersect, and zero for a degenerate box. Asymmetric
    on purpose: the question is "how much of THIS value is covered?", so a
    large annotation over a small value can reach 1.0 while the same pair
    scores low the other way round.
    """
    ax, ay, aw, ah = box
    bx, by, bw, bh = other
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    dx = min(ax + aw, bx + bw) - max(ax, bx)
    dy = min(ay + ah, by + bh) - max(ay, by)
    if dx <= 0 or dy <= 0:
        return 0.0
    return (dx * dy) / (aw * ah)


def _grow(box: tuple[float, float, float, float], tol: float) -> tuple[float, float, float, float]:
    x, y, w, h = box
    return clamp_box((x - tol, y - tol, w + 2 * tol, h + 2 * tol))


def in_any_region(
    box: tuple[float, float, float, float],
    regions: list[dict[str, Any]],
    *,
    tolerance: float = BOX_TOLERANCE,
) -> bool:
    """Does ``box`` (page-normalised) lie inside at least one of the document's
    regions, within ``tolerance``? A box that overlaps ANY region counts — the
    regions come from Phase 1 and may be imprecise, and a slightly generous box
    is not a wrong value."""
    for region in regions:
        region_box = (
            float(region.get("x") or 0.0), float(region.get("y") or 0.0),
            float(region.get("w") or 0.0), float(region.get("h") or 0.0),
        )
        if boxes_overlap(box, _grow(region_box, tolerance)):
            return True
    return False


def unrotate_box(
    box: tuple[float, float, float, float], rotation_deg: int,
) -> tuple[float, float, float, float]:
    """Map an UPRIGHT page box back to RAW page coordinates.

    Only meaningful for the quarter turns Phase 1 records (0/90/180/270): the
    PDF text layer is always in raw page coordinates, so a text-layer search
    for a rotated page needs this inverse. For any other angle the mapping is
    not a quarter turn, so the box is returned unchanged and the caller should
    treat the result as unreliable.
    """
    x, y, w, h = box
    deg = int(rotation_deg or 0) % 360
    if deg == 0:
        return (x, y, w, h)
    if deg == 90:
        # upright (x,y) came from raw (y, 1-x): invert
        return clamp_box((y, 1.0 - (x + w), h, w))
    if deg == 180:
        return clamp_box((1.0 - (x + w), 1.0 - (y + h), w, h))
    if deg == 270:
        return clamp_box((1.0 - (y + h), x, h, w))
    return (x, y, w, h)


def text_in_box(
    page_words: list[tuple[float, float, float, float, str]],
    box_raw: tuple[float, float, float, float],
    *,
    page_w: float,
    page_h: float,
) -> str:
    """The text-layer words whose centre falls inside a RAW page box."""
    if page_w <= 0 or page_h <= 0:
        return ""
    x, y, w, h = box_raw
    x0, y0, x1, y1 = x * page_w, y * page_h, (x + w) * page_w, (y + h) * page_h
    inside: list[str] = []
    for wx0, wy0, wx1, wy1, word in page_words:
        cx, cy = (wx0 + wx1) / 2.0, (wy0 + wy1) / 2.0
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            inside.append(word)
    return " ".join(inside)
