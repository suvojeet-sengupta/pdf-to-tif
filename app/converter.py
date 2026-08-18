"""Page rendering. Runs inside the render worker processes."""
from __future__ import annotations

import io
import os
from dataclasses import dataclass

try:  # PyMuPDF >= 1.26 renamed the top-level module; `fitz` still works but warns.
    import pymupdf as fitz
except ImportError:  # pragma: no cover - older PyMuPDF releases
    import fitz

from PIL import Image

# Colour modes offered in the UI.
COLOR_MODES = ("color", "gray", "bw")

# Pillow TIFF compression names, keyed by the value the UI sends.
COMPRESSIONS = {
    "none": "raw",
    "lzw": "tiff_lzw",
    "packbits": "packbits",
    "deflate": "tiff_deflate",
}

# Bilevel pages always use CCITT G4 - it is both the fastest and the smallest
# option for 1-bit data, and the generic codecs cannot even be applied there.
BW_COMPRESSION = "group4"

# Grey level at or below which a bilevel pixel becomes black.
BW_THRESHOLD = 160

_DOC_CACHE: dict[str, fitz.Document] = {}


@dataclass(frozen=True)
class RenderSpec:
    pdf_path: str
    out_dir: str
    prefix: str
    dpi: int
    color_mode: str
    compression: str
    pad: int
    max_pixels: int = 120_000_000


def normalise_options(dpi, color_mode, compression, min_dpi, max_dpi, default_dpi):
    """Clamp user supplied options to something we are willing to render."""
    try:
        dpi = int(dpi)
    except (TypeError, ValueError):
        dpi = default_dpi
    dpi = max(min_dpi, min(max_dpi, dpi))

    color_mode = str(color_mode or "color").lower()
    if color_mode not in COLOR_MODES:
        color_mode = "color"

    compression = str(compression or "lzw").lower()
    if compression not in COMPRESSIONS:
        compression = "lzw"

    return dpi, color_mode, compression


def _get_doc(path: str) -> fitz.Document:
    """Keep one open document per worker process instead of reopening per page."""
    doc = _DOC_CACHE.get(path)
    if doc is None:
        # Only ever cache a single document so long-lived workers do not grow.
        for old in list(_DOC_CACHE.values()):
            try:
                old.close()
            except Exception:
                pass
        _DOC_CACHE.clear()
        doc = fitz.open(path)
        _DOC_CACHE[path] = doc
    return doc


def render_page(spec: RenderSpec, page_index: int) -> tuple[int, str, int]:
    """Render one PDF page to a TIFF file. Returns (index, filename, bytes)."""
    doc = _get_doc(spec.pdf_path)
    page = doc.load_page(page_index)

    zoom = spec.dpi / 72.0
    # Very large sheets at high DPI can ask for gigabytes of pixmap. Scale the
    # zoom down to a sane ceiling rather than letting the worker die.
    rect = page.rect
    pixels = (rect.width * zoom) * (rect.height * zoom)
    if spec.max_pixels and pixels > spec.max_pixels:
        zoom *= (spec.max_pixels / pixels) ** 0.5

    matrix = fitz.Matrix(zoom, zoom)
    grayscale = spec.color_mode in ("gray", "bw")
    colorspace = fitz.csGRAY if grayscale else fitz.csRGB

    pix = page.get_pixmap(matrix=matrix, colorspace=colorspace, alpha=False)
    image = Image.frombytes(
        "L" if grayscale else "RGB", (pix.width, pix.height), pix.samples
    )
    pix = None  # release the pixmap buffer as early as possible

    if spec.color_mode == "bw":
        # A fixed threshold keeps scanned text crisp; dithering would smear it.
        image = image.point(lambda v: 255 if v > BW_THRESHOLD else 0, mode="1")
        compression = BW_COMPRESSION
    else:
        compression = COMPRESSIONS[spec.compression]

    filename = f"{spec.prefix}_page_{page_index + 1:0{spec.pad}d}.tif"
    out_path = os.path.join(spec.out_dir, filename)

    buffer = io.BytesIO()
    effective_dpi = max(1, round(72 * zoom))
    image.save(
        buffer,
        format="TIFF",
        compression=compression,
        dpi=(effective_dpi, effective_dpi),
    )
    data = buffer.getvalue()
    with open(out_path, "wb") as handle:
        handle.write(data)

    image.close()
    return page_index, filename, len(data)


def inspect_pdf(path: str) -> tuple[int, bool]:
    """Return (page_count, needs_password) without rendering anything."""
    with fitz.open(path) as doc:
        if doc.needs_pass:
            return 0, True
        return doc.page_count, False
