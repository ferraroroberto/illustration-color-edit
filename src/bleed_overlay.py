"""Composite trim / bleed / safety guides onto a CMYK soft-proof PNG.

Each book page has three concentric rectangles the prepress operator
cares about:

  * **Trim** — the cut line. Solid 1 px red.
  * **Bleed** — outside trim by ``bleed_inches``. Dashed magenta.
    Background art that should reach the page edge must extend to here.
  * **Safety** — inside trim by ``safety_inches``. Dashed cyan.
    Annotations and critical content should stay inside this.

The overlay turns a soft-proof PNG from "did the colors come out right?"
into a single image that *also* answers "is anything too close to the
cut line?".

This module mutates the PNG in place — the caller is responsible for
deciding whether to keep the un-overlaid version.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)


# Colors — chosen for visibility on a wide range of art backgrounds.
_TRIM_COLOR = (220, 38, 38, 255)      # solid red
_BLEED_COLOR = (217, 70, 239, 200)    # dashed magenta
_SAFETY_COLOR = (6, 182, 212, 200)    # dashed cyan
_DASH_LEN = 8
_GAP_LEN = 6


def _draw_dashed_rect(
    draw: ImageDraw.ImageDraw,
    bbox: tuple[int, int, int, int],
    color: tuple[int, int, int, int],
    width: int = 1,
) -> None:
    """Stroke the rectangle ``bbox`` with a dashed line.

    Pillow's ``rectangle`` only does solid lines, so we draw four sides
    as runs of short segments. Width is in pixels.
    """
    x0, y0, x1, y1 = bbox
    # Top side
    x = x0
    while x < x1:
        draw.line([(x, y0), (min(x + _DASH_LEN, x1), y0)], fill=color, width=width)
        x += _DASH_LEN + _GAP_LEN
    # Bottom side
    x = x0
    while x < x1:
        draw.line([(x, y1), (min(x + _DASH_LEN, x1), y1)], fill=color, width=width)
        x += _DASH_LEN + _GAP_LEN
    # Left side
    y = y0
    while y < y1:
        draw.line([(x0, y), (x0, min(y + _DASH_LEN, y1))], fill=color, width=width)
        y += _DASH_LEN + _GAP_LEN
    # Right side
    y = y0
    while y < y1:
        draw.line([(x1, y), (x1, min(y + _DASH_LEN, y1))], fill=color, width=width)
        y += _DASH_LEN + _GAP_LEN


def composite_guides(
    png_path: Path,
    *,
    trim_w_in: float,
    trim_h_in: float,
    bleed_in: float,
    safety_in: float,
    dpi: int,
) -> Path:
    """Draw trim / bleed / safety guides on top of ``png_path``.

    Mutates the PNG in place and returns the path back. The image is
    opened with PIL, drawn over with :class:`ImageDraw.Draw`, and saved
    back as PNG. Sizes are derived from the configured DPI:

      * The image is assumed to render the full PDF MediaBox = trim
        + bleed on every side. When ``bleed_in`` is 0 the trim line
        sits at the image edge.
      * The bleed box is the entire image (when bleed > 0); the trim
        box is inset by ``bleed_in × dpi`` pixels; the safety box is
        further inset by ``safety_in × dpi`` pixels.

    Failures (corrupt PNG, unwritable path) are logged and re-raised.
    """
    png_path = Path(png_path)
    if not png_path.is_file():
        raise FileNotFoundError(f"PNG not found: {png_path}")

    img = Image.open(png_path).convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size

    bleed_px = max(0, round(bleed_in * dpi))
    safety_px = max(0, round(safety_in * dpi))

    # Trim box — inset by the bleed amount.
    trim_box = (bleed_px, bleed_px, w - 1 - bleed_px, h - 1 - bleed_px)

    # Bleed box — only meaningful when there's actual bleed; otherwise
    # it would coincide with trim and add visual noise.
    if bleed_px > 0:
        bleed_box = (0, 0, w - 1, h - 1)
        _draw_dashed_rect(draw, bleed_box, _BLEED_COLOR, width=1)

    # Trim — solid.
    draw.rectangle(trim_box, outline=_TRIM_COLOR, width=1)

    # Safety — only when it actually fits inside the trim box.
    safety_box = (
        trim_box[0] + safety_px,
        trim_box[1] + safety_px,
        trim_box[2] - safety_px,
        trim_box[3] - safety_px,
    )
    if safety_box[2] > safety_box[0] and safety_box[3] > safety_box[1]:
        _draw_dashed_rect(draw, safety_box, _SAFETY_COLOR, width=1)

    # Save back as PNG. Convert to RGB first if the source had no alpha
    # so the file size stays comparable.
    out = img if img.mode == "RGBA" else img.convert("RGB")
    out.save(png_path, format="PNG")
    log.debug(
        "Drew guides on %s (trim=%.2fx%.2f in, bleed=%.2f, safety=%.2f, dpi=%d)",
        png_path.name, trim_w_in, trim_h_in, bleed_in, safety_in, dpi,
    )
    return png_path


__all__ = ["composite_guides"]
