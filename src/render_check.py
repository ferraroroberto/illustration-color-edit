"""Detect Inkscape SVG→PDF export discrepancies via a render-truth diff.

Inkscape's vector PDF backend occasionally mis-composites overlapping
*same-colour* shapes. The concrete case that motivated this module (issue
#8): an emoji eye drawn as a small filled dot stacked over another fill is
dropped in the PDF — only a thin sliver of an underlying element shows —
while the very same SVG renders correctly in Inkscape's own raster (PNG)
export and in browsers. Because the corruption lands in the *vector* PDF,
it is baked into the print deliverable, not just the soft-proof.

There is no clean way to make Inkscape's PDF writer behave, and the
practical fixes (swap to a vector renderer that needs awkward native libs
on Windows, or rasterise the page and lose vector output) were rejected in
favour of keeping the existing Inkscape→Ghostscript vector pipeline and
*flagging* the affected illustrations so the artwork can be reworked in
Affinity (merge or nudge the stacked shapes, or flatten that group).

Detection is renderer-truth based rather than a geometric heuristic: we do
not try to predict which overlaps will trip the bug. Instead we render the
(already page-sized) SVG to PNG with Inkscape — the ground truth — and the
RGB PDF that Inkscape produced to PNG with Ghostscript, then diff the two.
Edge anti-aliasing differs between the two rasterisers, so the binary diff
mask is eroded to drop 1–2 px edge noise; only solid blocks above a minimum
area survive and are reported as discrepancy regions with their location on
the page. Both renders happen *before* the CMYK colour conversion so only
structural differences show, never the expected ICC colour shift.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageFilter

from .cmyk_convert import _gs_png_render_argv, _resolve_ghostscript
from .svg_to_pdf import _resolve_inkscape

log = logging.getLogger(__name__)

# Tuned for flat-colour book illustrations rendered at ``_DEFAULT_DPI``.
# Rasteriser anti-aliasing differences are 1–2 px wide edges/specks; a
# single 1 px erosion (MinFilter size 3) erases them along with any thin
# misalignment line, leaving only solid blobs. A dropped emoji-dot-sized
# shape on a letterboxed 2×2 page survives at ~30 px (300 dpi), so the
# minimum area sits well below that and well above the eroded noise floor
# (≈0). The channel threshold rejects mild rasteriser colour drift.
_DEFAULT_DPI = 300
_CHANNEL_THRESHOLD = 96   # min per-channel 0–255 delta to count a pixel as "different"
_ERODE_PX = 1             # morphological erosion radius applied to the diff mask
_MIN_REGION_PX = 16       # discard surviving blobs smaller than this (post-erosion)
_MAX_REGIONS = 6          # cap reported regions so the warning stays readable


class RenderCheckError(RuntimeError):
    """Raised when a render needed for the fidelity check could not be produced."""


@dataclass
class DiffRegion:
    """A solid region where the PDF render diverges from the reference render."""

    x0: int
    y0: int
    x1: int
    y1: int
    area_px: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0 + 1

    @property
    def height(self) -> int:
        return self.y1 - self.y0 + 1

    def center_pct(self, page_w_px: int, page_h_px: int) -> tuple[float, float]:
        """Region centre as (x%, y%) of the page — used to locate it in Affinity."""
        cx = (self.x0 + self.x1) / 2.0
        cy = (self.y0 + self.y1) / 2.0
        return (
            100.0 * cx / page_w_px if page_w_px else 0.0,
            100.0 * cy / page_h_px if page_h_px else 0.0,
        )


@dataclass
class RenderCheckReport:
    """Outcome of one SVG-vs-PDF fidelity check."""

    regions: list[DiffRegion] = field(default_factory=list)
    page_w_px: int = 0
    page_h_px: int = 0

    @property
    def has_discrepancy(self) -> bool:
        return bool(self.regions)

    def summary(self) -> str:
        """Human-readable, LLM/operator-friendly one-liner for the warning list.

        A single dropped shape can split into a couple of adjacent diff blobs
        (e.g. a dot bisected by the sliver showing through), so the listed
        page-relative locations are de-duplicated to whole-percent spots — the
        operator only needs *where* to look, not a literal blob count.
        """
        if not self.regions:
            return "no PDF render discrepancy detected"
        seen: list[str] = []
        for r in self.regions:
            x, y = r.center_pct(self.page_w_px, self.page_h_px)
            spot = f"{x:.0f}%,{y:.0f}%"
            if spot not in seen:
                seen.append(spot)
        n = len(seen)
        return (
            f"PDF export differs from the reference render at {n} "
            f"location{'s' if n != 1 else ''} (page-relative: {', '.join(seen)}) "
            "— likely Inkscape dropping a stacked same-colour shape "
            "(issue #8); the printed PDF is wrong here. Rework in Affinity: "
            "merge or nudge the overlapping shapes, or flatten that group."
        )


def _render_svg_png(svg_path: Path, png_path: Path, dpi: int, inkscape_exe: str) -> None:
    """Render an SVG to an opaque-white-background PNG with Inkscape (ground truth)."""
    bin_path = _resolve_inkscape(inkscape_exe)
    cmd = [
        bin_path,
        str(svg_path),
        "--export-type=png",
        f"--export-filename={png_path}",
        "--export-area-page",
        f"--export-dpi={int(dpi)}",
        "--export-background=#ffffff",
        "--export-background-opacity=1",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if result.returncode != 0 or not png_path.is_file():
        raise RenderCheckError(
            f"Inkscape SVG→PNG failed (exit {result.returncode}) on {svg_path.name}: "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )


def _render_pdf_png(pdf_path: Path, png_path: Path, dpi: int, gs_exe: str) -> None:
    """Render the first PDF page to PNG with Ghostscript (the export under test)."""
    bin_path = _resolve_ghostscript(gs_exe)
    cmd = _gs_png_render_argv(bin_path, pdf_path, png_path, dpi)
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if result.returncode != 0 or not png_path.is_file():
        raise RenderCheckError(
            f"Ghostscript PDF→PNG failed (exit {result.returncode}) on {pdf_path.name}: "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )


def _connected_components(mask: np.ndarray) -> list[DiffRegion]:
    """Label 4-connected True blobs in ``mask``; return one DiffRegion per blob.

    Pure NumPy + an iterative flood fill (no SciPy dependency). Only the
    already-sparse set of True pixels is visited, so this is cheap after the
    diff mask has been thresholded and eroded.
    """
    h, w = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    regions: list[DiffRegion] = []
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if visited[sy, sx]:
            continue
        stack = [(sy, sx)]
        visited[sy, sx] = True
        minx = maxx = sx
        miny = maxy = sy
        area = 0
        while stack:
            cy, cx = stack.pop()
            area += 1
            if cx < minx:
                minx = cx
            elif cx > maxx:
                maxx = cx
            if cy < miny:
                miny = cy
            elif cy > maxy:
                maxy = cy
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))
        regions.append(DiffRegion(minx, miny, maxx, maxy, area))
    return regions


def find_solid_diff_regions(
    reference: np.ndarray,
    test: np.ndarray,
    *,
    channel_threshold: int = _CHANNEL_THRESHOLD,
    erode_px: int = _ERODE_PX,
    min_region_px: int = _MIN_REGION_PX,
    max_regions: int = _MAX_REGIONS,
) -> list[DiffRegion]:
    """Return solid regions where ``test`` diverges from ``reference``.

    Both inputs are HxWx3 uint8 RGB arrays. ``test`` is resized to the
    reference shape if they differ by a rounding pixel. The per-pixel max
    channel delta is thresholded, the binary mask is eroded to discard
    anti-aliasing edge noise, and surviving 4-connected blobs at or above
    ``min_region_px`` are returned, largest first, capped at ``max_regions``.

    Pure function — no I/O — so the diff logic is unit-testable without
    invoking Inkscape or Ghostscript.
    """
    if reference.shape[:2] != test.shape[:2]:
        test = np.asarray(
            Image.fromarray(test).resize(
                (reference.shape[1], reference.shape[0]), Image.NEAREST
            )
        )
    ref = reference.astype(np.int16)
    tst = test.astype(np.int16)
    delta = np.abs(ref - tst).max(axis=2)
    mask = delta > channel_threshold
    if not mask.any():
        return []

    if erode_px > 0:
        mask_img = Image.fromarray((mask * 255).astype(np.uint8), mode="L")
        eroded = mask_img.filter(ImageFilter.MinFilter(2 * erode_px + 1))
        mask = np.asarray(eroded) > 0
        if not mask.any():
            return []

    regions = [r for r in _connected_components(mask) if r.area_px >= min_region_px]
    regions.sort(key=lambda r: r.area_px, reverse=True)
    return regions[:max_regions]


def check_render_fidelity(
    reference_svg: Path,
    rgb_pdf: Path,
    *,
    inkscape_exe: str = "inkscape",
    gs_exe: str = "gswin64c",
    dpi: int = _DEFAULT_DPI,
    channel_threshold: int = _CHANNEL_THRESHOLD,
    erode_px: int = _ERODE_PX,
    min_region_px: int = _MIN_REGION_PX,
    max_regions: int = _MAX_REGIONS,
) -> RenderCheckReport:
    """Compare an SVG's reference render against the RGB PDF Inkscape produced.

    Renders ``reference_svg`` to PNG with Inkscape (correct) and ``rgb_pdf``
    to PNG with Ghostscript (the export under test) at the same page size,
    then reports solid regions where they diverge — the signature of the
    Inkscape PDF-export shape-dropping bug (issue #8).

    :param reference_svg: the page-sized SVG that ``rgb_pdf`` was exported
        from (so both renders share geometry).
    :param rgb_pdf: the RGB PDF produced by :func:`src.svg_to_pdf.svg_to_pdf`.
    :raises RenderCheckError: if either render cannot be produced.
    """
    reference_svg = Path(reference_svg)
    rgb_pdf = Path(rgb_pdf)
    if not reference_svg.is_file():
        raise RenderCheckError(f"reference SVG not found: {reference_svg}")
    if not rgb_pdf.is_file():
        raise RenderCheckError(f"RGB PDF not found: {rgb_pdf}")

    with tempfile.TemporaryDirectory(prefix="rendercheck_") as td:
        ref_png = Path(td) / "ref.png"
        test_png = Path(td) / "test.png"
        _render_svg_png(reference_svg, ref_png, dpi, inkscape_exe)
        _render_pdf_png(rgb_pdf, test_png, dpi, gs_exe)
        with Image.open(ref_png) as r, Image.open(test_png) as t:
            ref = np.asarray(r.convert("RGB"))
            test = np.asarray(t.convert("RGB"))

    regions = find_solid_diff_regions(
        ref,
        test,
        channel_threshold=channel_threshold,
        erode_px=erode_px,
        min_region_px=min_region_px,
        max_regions=max_regions,
    )
    return RenderCheckReport(
        regions=regions, page_w_px=ref.shape[1], page_h_px=ref.shape[0]
    )
