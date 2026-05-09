"""Total Area Coverage (TAC) check for CMYK PDFs.

Most book printers cap total ink coverage at ~300–340% on coated paper and
240–260% on uncoated. The sum is across all four channels: a saturated
deep red after ICC conversion can easily land at C70/M100/Y100/K30 = 300%
which is at the limit; a "rich black" at C40/M40/Y40/K100 = 220% is fine
for coated but a problem on uncoated. Files that exceed the publisher's
TAC limit get rejected by prepress.

This module renders the produced CMYK PDF to a 4-channel raster (one
sample per pixel per channel) via Ghostscript and computes:

  * Per-pixel max TAC.
  * Per-pixel mean TAC.
  * 99th percentile TAC.
  * Fraction of pixels exceeding the threshold.

The check is read-only — it doesn't modify the PDF. The caller decides
what to do with the result (warn, fail the batch, surface in the QA
report, etc.).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .cmyk_convert import GhostscriptNotFoundError, _resolve_ghostscript

log = logging.getLogger(__name__)


@dataclass
class TacReport:
    """Per-page TAC measurements for one CMYK PDF."""

    max_pct: float
    """Highest single-pixel TAC found, in percent (0–400)."""
    mean_pct: float
    """Average TAC across all sampled pixels, in percent (0–400)."""
    p99_pct: float
    """99th percentile TAC — robust max, immune to one-pixel outliers."""
    violation_fraction: float
    """Fraction of pixels at or above the threshold (0.0–1.0)."""
    threshold_pct: float
    """The TAC limit applied for the violation count."""

    @property
    def status(self) -> str:
        """One-letter status: ``ok`` / ``warn`` / ``fail``.

        * ``ok``   — no pixel violates the threshold.
        * ``warn`` — under 0.1% of pixels violate (likely a few stray
          fully-saturated pixels at edges).
        * ``fail`` — meaningful region is over the limit.
        """
        if self.violation_fraction <= 0.0:
            return "ok"
        if self.violation_fraction < 0.001:
            return "warn"
        return "fail"


class TacComputeError(RuntimeError):
    """Raised when Ghostscript fails to render the TAC raster."""


def _render_cmyk_tiff(
    pdf_path: Path,
    tiff_path: Path,
    gs_exe: str,
    dpi: int,
) -> None:
    """Rasterize ``pdf_path`` to a 4-channel CMYK TIFF at ``dpi``.

    Uses Ghostscript's ``tiff32nc`` device — packed 32-bit CMYK, one byte
    per channel, no compression. PIL reads it natively as mode "CMYK"
    so we can convert to numpy without any colorspace gymnastics.
    """
    bin_path = _resolve_ghostscript(gs_exe)
    cmd = [
        bin_path,
        "-dNOPAUSE",
        "-dBATCH",
        "-dSAFER",
        "-sDEVICE=tiff32nc",
        f"-r{dpi}",
        "-dFirstPage=1",
        "-dLastPage=1",
        f"-sOutputFile={tiff_path}",
        str(pdf_path),
    ]
    log.debug("Ghostscript TAC raster: %s", cmd)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not tiff_path.is_file():
        raise TacComputeError(
            f"Ghostscript tiff32nc failed (exit {result.returncode}) on "
            f"{pdf_path.name}: "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )


def compute_tac(
    pdf_path: Path,
    gs_exe: str = "gswin64c",
    dpi: int = 100,
    threshold_pct: float = 320.0,
) -> TacReport:
    """Compute :class:`TacReport` for a CMYK PDF.

    :param pdf_path: a CMYK PDF (the output of the pipeline).
    :param gs_exe: Ghostscript binary path or name on PATH.
    :param dpi: rendering resolution. 100 dpi is enough for accurate
        per-pixel coverage on book illustrations; raise to 150–200 if
        the printer's spec is very tight or the artwork has hairline
        features. The trade-off is render time and RAM.
    :param threshold_pct: TAC limit in percent (typically 240–340).

    :raises GhostscriptNotFoundError: if Ghostscript is unavailable.
    :raises TacComputeError: if the rasterizer fails for any reason.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    # Cheap availability check up front — same exception shape as
    # cmyk_convert so callers handle one error class.
    _ = _resolve_ghostscript(gs_exe)  # noqa: F841

    with tempfile.TemporaryDirectory(prefix="tac_") as td:
        tiff = Path(td) / "page1.tif"
        _render_cmyk_tiff(pdf_path, tiff, gs_exe, dpi)
        with Image.open(tiff) as img:
            if img.mode != "CMYK":
                raise TacComputeError(
                    f"Expected CMYK TIFF, got mode {img.mode!r} for {pdf_path.name}."
                )
            arr = np.asarray(img, dtype=np.uint16)  # (H, W, 4)

    # Each channel byte is 0–255, where 255 = 100% ink. Sum across
    # channels then convert to percent (max possible = 4*255 = 1020 = 400%).
    sums = arr.sum(axis=2)  # (H, W) uint16, max 1020.
    pct = sums.astype(np.float32) * (100.0 / 255.0)
    max_pct = float(pct.max())
    mean_pct = float(pct.mean())
    p99_pct = float(np.percentile(pct, 99.0))
    violation_count = int((pct >= threshold_pct).sum())
    total = pct.size
    fraction = violation_count / total if total else 0.0

    return TacReport(
        max_pct=max_pct,
        mean_pct=mean_pct,
        p99_pct=p99_pct,
        violation_fraction=fraction,
        threshold_pct=threshold_pct,
    )


# Re-export so callers don't have to import from cmyk_convert too.
__all__ = ["TacReport", "TacComputeError", "GhostscriptNotFoundError", "compute_tac"]
