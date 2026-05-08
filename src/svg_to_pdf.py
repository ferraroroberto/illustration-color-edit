"""SVG → RGB PDF conversion via the Inkscape CLI.

This is the first stage of the CMYK print pipeline. Inkscape produces an
RGB PDF at the requested page size; the CMYK conversion happens downstream
in :mod:`src.cmyk_convert`.

We pick Inkscape over cairosvg for two reasons:
  1. Inkscape is already a project dependency (used for PNG export of the
     grayscale workflow), so no new system requirement.
  2. Inkscape consistently round-trips Affinity Designer SVGs (gradients,
     embedded rasters, filters) better than cairosvg in our experience.

See ``docs/2026-05-07-cmyk-pipeline.md`` for the full rationale.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Inkscape's CLI uses 96 user units = 1 inch by default for non-page exports;
# for --export-area-page, dimensions come from the SVG's viewBox, so we set
# them explicitly via --export-width / --export-height in pixels at 96 DPI.
PIXELS_PER_INCH = 96.0


class InkscapeNotFoundError(RuntimeError):
    """Raised when the Inkscape binary cannot be located or invoked."""


class SvgToPdfError(RuntimeError):
    """Raised when Inkscape exits non-zero during PDF export."""


def _resolve_inkscape(inkscape_exe: str) -> str:
    """Return a runnable Inkscape path or raise :class:`InkscapeNotFoundError`."""
    if Path(inkscape_exe).is_file():
        return inkscape_exe
    found = shutil.which(inkscape_exe)
    if found:
        return found
    raise InkscapeNotFoundError(
        f"Inkscape binary not found: {inkscape_exe!r}. "
        "Install Inkscape (https://inkscape.org) and set "
        "`png_export.inkscape_path` in config.json if it is not on PATH."
    )


def svg_to_pdf(
    svg_path: Path,
    pdf_path: Path,
    width_inches: float,
    height_inches: float,
    bleed_inches: float = 0.0,
    inkscape_exe: str = "inkscape",
) -> Path:
    """Convert ``svg_path`` to an RGB PDF at the target page dimensions.

    The rendered PDF page is ``(width + 2*bleed) x (height + 2*bleed)`` inches.
    Bleed is added symmetrically on all sides; the SVG content is scaled to
    fill the trim box, so authoring SVGs should already include any bleed
    artwork at the edges (the trim itself is not drawn).

    :param svg_path: source SVG file (must exist).
    :param pdf_path: destination PDF (parent directory created if missing).
    :param width_inches: trim width in inches.
    :param height_inches: trim height in inches.
    :param bleed_inches: bleed added on each side, in inches.
    :param inkscape_exe: path to inkscape binary, or name on PATH.
    :returns: ``pdf_path`` (resolved).
    :raises InkscapeNotFoundError: if Inkscape is not found.
    :raises SvgToPdfError: if Inkscape exits non-zero.
    :raises FileNotFoundError: if ``svg_path`` does not exist.
    """
    svg_path = Path(svg_path)
    pdf_path = Path(pdf_path)
    if not svg_path.is_file():
        raise FileNotFoundError(f"SVG not found: {svg_path}")
    if width_inches <= 0 or height_inches <= 0:
        raise ValueError(f"width and height must be > 0 (got {width_inches}, {height_inches})")
    if bleed_inches < 0:
        raise ValueError(f"bleed_inches must be >= 0 (got {bleed_inches})")

    bin_path = _resolve_inkscape(inkscape_exe)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    page_w_in = width_inches + 2 * bleed_inches
    page_h_in = height_inches + 2 * bleed_inches
    width_px = int(round(page_w_in * PIXELS_PER_INCH))
    height_px = int(round(page_h_in * PIXELS_PER_INCH))

    cmd = [
        bin_path,
        str(svg_path),
        "--export-type=pdf",
        f"--export-filename={pdf_path}",
        "--export-area-page",
        f"--export-width={width_px}",
        f"--export-height={height_px}",
    ]
    log.info("Inkscape SVG→PDF: %s → %s (%.2f×%.2f in, bleed %.3f)",
             svg_path.name, pdf_path.name, width_inches, height_inches, bleed_inches)
    log.debug("Inkscape command: %s", cmd)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SvgToPdfError(
            f"Inkscape failed (exit {result.returncode}) on {svg_path.name}: "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )
    if not pdf_path.is_file():
        raise SvgToPdfError(
            f"Inkscape returned 0 but {pdf_path} was not produced. "
            f"stderr={result.stderr.strip()!r}"
        )
    return pdf_path
