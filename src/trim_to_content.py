"""Trim an SVG to a tight bounding box around its visible content.

Slots into the CMYK pipeline between palette remap and PDF conversion: the
publisher places illustrations into a book layout and doesn't want padding
around the artwork. When trim-to-content is on the resulting PDF page is
the artwork's actual extent (plus optional padding), not the configured
trim size.

Bbox is computed by **rendering** the SVG to a transparent PNG and reading
the alpha bbox via Pillow. Earlier attempts using ``svgelements`` and then
``inkscape -S`` both reported *geometric* bboxes — stroke extents and text
anti-aliasing run a few pixels past those, which produced PDFs that
clipped the outer strokes / character edges. The render-then-detect
approach captures whatever the renderer actually drew, so what we crop to
is what the eye sees: strokes, anti-aliased text, filter effects, all
included automatically.

Coordinate spaces:
- The render is at ``RENDER_DPI`` over the SVG's full viewport, so 1 px in
  the rendered PNG = ``96 / RENDER_DPI`` SVG user units.
- The trimmed ``viewBox`` is rewritten in the original SVG's viewBox
  coordinate system (user units), so the artwork's path geometry inside
  the document never has to move.
- ``width`` / ``height`` are written in inches assuming SVG user units are
  96-per-inch (Affinity Designer's default export) so Inkscape produces a
  PDF page at the artwork's natural physical extent.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image
from lxml import etree

log = logging.getLogger(__name__)

# SVG default user-units-per-inch. Affinity exports use this.
_PPI = 96.0
# 1pt = 1/72 in. Padding is taken in points to match the publisher's
# vocabulary; converted to user units (= px at default 96 DPI) so it can
# be added to the bbox in the same coordinate space.
_PT_PER_INCH = 72.0
# Rendering resolution for bbox detection. Trade-off: higher = sub-pixel
# precision on the bbox edge, lower = faster + less RAM. 200 catches
# 0.48-pt details (1 px = 0.36 pt at 200 DPI) which is finer than any
# stroke we expect to encounter and is plenty for trimming.
_RENDER_DPI = 200.0


class TrimError(RuntimeError):
    """Raised when the Inkscape bbox query fails."""


@dataclass
class TrimReport:
    """Outcome of a trim attempt for one SVG.

    ``had_content=False`` means no visible drawn elements were found; the
    output file is a byte-copy of the input and the caller should fall back
    to the configured trim size.
    """

    original_viewbox: str
    new_viewbox: str
    width_in: float
    height_in: float
    padding_pt: float
    had_content: bool


def _resolve_inkscape(inkscape_exe: str) -> str:
    """Return a runnable Inkscape path or raise :class:`TrimError`."""
    if Path(inkscape_exe).is_file():
        return inkscape_exe
    found = shutil.which(inkscape_exe)
    if found:
        return found
    raise TrimError(
        f"Inkscape binary not found: {inkscape_exe!r}. "
        "Trim-to-content requires Inkscape for accurate bbox computation."
    )


def _read_viewbox(svg_path: Path) -> tuple[float, float, float, float]:
    """Return the SVG root's viewBox as ``(x, y, w, h)``.

    If the SVG has no explicit viewBox the box is synthesised from the
    root ``width`` / ``height`` attributes (the SVG default behaviour).
    Falls back to ``(0, 0, 0, 0)`` if neither is present.
    """
    root = etree.parse(str(svg_path)).getroot()
    raw = root.get("viewBox")
    if raw:
        parts = raw.replace(",", " ").split()
        if len(parts) == 4:
            try:
                return tuple(float(p) for p in parts)  # type: ignore[return-value]
            except ValueError:
                pass
    # Fall back to width/height attributes, stripping any unit suffix.
    def _strip_unit(v: Optional[str]) -> float:
        if not v:
            return 0.0
        s = v.strip().lower()
        for u in ("px", "pt", "in", "mm", "cm", "%"):
            if s.endswith(u):
                s = s[: -len(u)]
                break
        try:
            return float(s)
        except ValueError:
            return 0.0
    return (0.0, 0.0, _strip_unit(root.get("width")), _strip_unit(root.get("height")))


def _query_drawing_bbox(
    svg_path: Path, inkscape_exe: str,
) -> Optional[tuple[float, float, float, float]]:
    """Return ``(xmin, ymin, xmax, ymax)`` of the rendered drawing in user units.

    Approach: render the SVG to a transparent PNG at ``_RENDER_DPI`` over
    its full viewport, then ask Pillow for the alpha bbox of non-zero
    pixels. This catches stroke extents and text anti-aliasing that
    ``inkscape -S`` (geometric bbox) misses by several pixels.

    Returns ``None`` when the rendered image is fully transparent — i.e.
    the SVG has no visible drawn content.
    """
    bin_path = _resolve_inkscape(inkscape_exe)
    vb_x, vb_y, vb_w, vb_h = _read_viewbox(svg_path)
    if vb_w <= 0 or vb_h <= 0:
        raise TrimError(
            f"{svg_path.name}: cannot determine viewBox dimensions for trim."
        )

    with tempfile.TemporaryDirectory(prefix="trim_") as tmpdir:
        png = Path(tmpdir) / "render.png"
        # ``--export-area-page`` keeps the PNG's coordinate system aligned
        # with the SVG viewBox (origin at viewBox.{x,y}, scale = DPI/96)
        # so pixel→user-unit conversion is a single scalar.
        cmd = [
            bin_path,
            "--export-type=png",
            f"--export-filename={png}",
            "--export-area-page",
            f"--export-dpi={int(_RENDER_DPI)}",
            "--export-background-opacity=0",  # transparent bg
            str(svg_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not png.is_file():
            raise TrimError(
                f"Inkscape render for trim failed on {svg_path.name}: "
                f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
            )

        im = Image.open(png)
        # Force RGBA so getbbox() acts on the alpha channel for fully
        # transparent regions. Non-alpha modes treat (0,0,0) as zero which
        # would skip pure-black ink — wrong.
        if im.mode != "RGBA":
            im = im.convert("RGBA")
        # getbbox returns the bounding box of non-zero (any channel) pixels;
        # on the alpha channel of a transparent-background render this is
        # the exact rendered extent of the drawing, anti-aliased edges
        # included.
        pixel_bbox = im.getbbox()
        if pixel_bbox is None:
            return None
        px_x0, px_y0, px_x1, px_y1 = pixel_bbox
        png_w, png_h = im.size

    # Pixel → user-unit conversion. The PNG covers exactly the viewBox at
    # _RENDER_DPI; one PNG pixel spans (viewBox_dim / png_dim) user units.
    sx = vb_w / png_w
    sy = vb_h / png_h
    xmin = vb_x + px_x0 * sx
    ymin = vb_y + px_y0 * sy
    xmax = vb_x + px_x1 * sx
    ymax = vb_y + px_y1 * sy
    return xmin, ymin, xmax, ymax


def compute_content_bbox(
    svg_path: Path | str,
    inkscape_exe: str = "inkscape",
) -> Optional[tuple[float, float, float, float]]:
    """Return the visible-content bbox of ``svg_path`` in SVG user units.

    ``None`` is returned for an SVG with no visible drawn elements.
    """
    return _query_drawing_bbox(Path(svg_path), inkscape_exe)


def _format_viewbox(x: float, y: float, w: float, h: float) -> str:
    """Render a viewBox tuple compactly, dropping needless trailing zeros."""
    def fmt(v: float) -> str:
        # 4 decimals is plenty for sub-pixel precision; trim trailing zeros.
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return s or "0"
    return f"{fmt(x)} {fmt(y)} {fmt(w)} {fmt(h)}"


def trim_svg_to_content(
    input_path: Path | str,
    output_path: Path | str,
    padding_pt: float = 0.0,
    inkscape_exe: str = "inkscape",
) -> TrimReport:
    """Rewrite an SVG so its viewBox + width/height match its visible content.

    The artwork's path coordinates and structure are left untouched — only
    the root ``viewBox`` / ``width`` / ``height`` are rewritten. The output
    ``width`` / ``height`` are in inches (``Nin``) so Inkscape produces a
    PDF page at the artwork's natural physical size.

    If the SVG has no visible drawn content, the input is copied through
    unchanged and ``TrimReport.had_content`` is False.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    tree = etree.parse(str(input_path))
    root = tree.getroot()
    original_viewbox = root.get("viewBox", "")

    bbox = _query_drawing_bbox(input_path, inkscape_exe)
    if bbox is None:
        # No content — copy file as-is. The caller decides whether to warn.
        if input_path.resolve() != output_path.resolve():
            shutil.copyfile(input_path, output_path)
        log.warning("trim: %s has no visible content; passing through unchanged",
                    input_path.name)
        return TrimReport(
            original_viewbox=original_viewbox,
            new_viewbox=original_viewbox,
            width_in=0.0,
            height_in=0.0,
            padding_pt=padding_pt,
            had_content=False,
        )

    xmin, ymin, xmax, ymax = bbox
    # Padding is given in points; convert to user units (= 96-DPI px) to
    # apply to the bbox in the same coordinate space.
    pad = padding_pt * (_PPI / _PT_PER_INCH)
    xmin -= pad
    ymin -= pad
    xmax += pad
    ymax += pad

    new_vb_w = max(xmax - xmin, 0.0)
    new_vb_h = max(ymax - ymin, 0.0)
    # User units → inches at 96 DPI. Affinity defaults; we don't honour a
    # non-standard PPI here.
    width_in = new_vb_w / _PPI
    height_in = new_vb_h / _PPI
    new_viewbox = _format_viewbox(xmin, ymin, new_vb_w, new_vb_h)

    root.set("viewBox", new_viewbox)
    root.set("width", f"{width_in}in")
    root.set("height", f"{height_in}in")
    # Match _apply_page_size's posture: avoid distortion if downstream tools
    # ever interpret width/height differently from viewBox.
    par = (root.get("preserveAspectRatio") or "").strip().lower()
    if not par or par == "none":
        root.set("preserveAspectRatio", "xMidYMid meet")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(
        str(output_path),
        xml_declaration=True,
        encoding="utf-8",
        standalone=False,
    )

    return TrimReport(
        original_viewbox=original_viewbox,
        new_viewbox=new_viewbox,
        width_in=width_in,
        height_in=height_in,
        padding_pt=padding_pt,
        had_content=True,
    )
