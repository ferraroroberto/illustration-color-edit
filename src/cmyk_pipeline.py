"""CMYK print export orchestrator.

Wires together the three stages of the pipeline for one or many SVGs:

    1. Apply RGB→RGB pre-correction (reuses :func:`svg_writer.apply_mapping_with_report`).
    2. SVG → RGB PDF (Inkscape, via :mod:`src.svg_to_pdf`).
    3. RGB PDF → CMYK PDF (Ghostscript + ICC, via :mod:`src.cmyk_convert`).
    4. (Optional) CMYK PDF → soft-proof PNG.

Per-file errors are caught and recorded in the per-batch report so a single
broken SVG never kills a batch run.

The pipeline also detects two SVG features that warrant a publisher warning:

  * ``<image>`` elements — embedded raster bitmaps don't get re-color-managed
    by the SVG-level correction step; they pass through to Ghostscript which
    will ICC-convert them but the user should know.
  * ``<text>`` elements — should normally be outlined to paths in Affinity
    before export so fonts don't have to be embedded in the CMYK PDF.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from lxml import etree

from .cmyk_convert import (
    CmykConvertError,
    GhostscriptNotFoundError,
    IccProfileNotFoundError,
    pdf_to_preview_png,
    rgb_pdf_to_cmyk,
)
from .svg_parser import _localname, parse_svg
from .svg_to_pdf import InkscapeNotFoundError, SvgToPdfError, svg_to_pdf
from .svg_writer import apply_mapping_with_report

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Context & result types
# --------------------------------------------------------------------------- #
@dataclass
class CmykContext:
    """Everything :func:`process_one` needs to convert a single SVG.

    Constructed once per batch run from :class:`AppConfig.cmyk_export` plus
    the merged correction mapping (global + per-illustration overrides).
    """

    output_dir: Path
    icc_profile: Path
    inkscape_exe: str
    ghostscript_exe: str
    width_inches: float
    height_inches: float
    bleed_inches: float = 0.0
    pdfx: bool = False
    generate_preview: bool = True
    preview_dpi: int = 150
    tmp_dir: Optional[Path] = None  # defaults to output_dir / "_tmp"

    def resolved_tmp_dir(self) -> Path:
        return self.tmp_dir or (self.output_dir / "_tmp")


@dataclass
class FileResult:
    """Per-file outcome row in the batch report."""

    filename: str
    status: str  # "ok" | "error"
    output_pdf: Optional[Path] = None
    preview_png: Optional[Path] = None
    replacements: int = 0
    unmapped_colors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None
    elapsed_seconds: float = 0.0


@dataclass
class BatchReport:
    """Aggregate result for a batch run."""

    started_at: str = ""
    finished_at: str = ""
    total_seconds: float = 0.0
    icc_profile: str = ""
    pdfx: bool = False
    width_inches: float = 0.0
    height_inches: float = 0.0
    bleed_inches: float = 0.0
    files: list[FileResult] = field(default_factory=list)
    palette: dict[str, int] = field(default_factory=dict)        # color hex → file count
    palette_mapped: dict[str, str] = field(default_factory=dict)  # source → corrected target

    @property
    def succeeded(self) -> int:
        return sum(1 for f in self.files if f.status == "ok")

    @property
    def failed(self) -> int:
        return sum(1 for f in self.files if f.status == "error")


# --------------------------------------------------------------------------- #
# SVG content warnings
# --------------------------------------------------------------------------- #
def _apply_page_size(svg_path: Path, width_in: float, height_in: float) -> None:
    """Patch the SVG root with physical width/height and a letterbox aspect rule.

    Inkscape's vector PDF export derives the PDF MediaBox from the SVG root's
    ``width`` / ``height`` attributes (raster ``--export-width`` /
    ``--export-height`` flags do not apply to PDF). Affinity exports
    typically write ``width="100%" height="100%"`` plus a ``viewBox``, which
    Inkscape interprets as user units at 96 DPI — producing a PDF page
    that's the wrong physical size.

    This helper rewrites width/height to the requested inches and ensures
    ``preserveAspectRatio="xMidYMid meet"`` so art with an aspect that
    doesn't match the page is letterboxed (centered, with margin on the
    short axis) rather than distorted.

    The viewBox is preserved untouched so the artwork's coordinate space is
    unchanged. Mutates ``svg_path`` in place; intended for use on the temp
    ``_corrected.svg`` files the pipeline already writes.
    """
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    root.set("width", f"{width_in}in")
    root.set("height", f"{height_in}in")
    par = (root.get("preserveAspectRatio") or "").strip().lower()
    if not par or par == "none":
        # Empty (relies on default — make explicit) or "none" (would distort).
        root.set("preserveAspectRatio", "xMidYMid meet")
    # Any other explicit author value is respected.
    tree.write(str(svg_path), xml_declaration=True, encoding="utf-8", standalone=False)


def detect_svg_warnings(svg_path: Path) -> list[str]:
    """Return a list of human-readable warnings about an SVG's content.

    Detected:
      * ``<image>`` elements (embedded raster bitmaps).
      * ``<text>`` elements (un-outlined text).
    """
    warnings: list[str] = []
    try:
        tree = etree.parse(str(svg_path))
    except (OSError, etree.XMLSyntaxError) as exc:
        return [f"could not parse SVG: {exc}"]

    has_image = False
    has_text = False
    for el in tree.getroot().iter():
        ln = _localname(el.tag)
        if ln == "image":
            has_image = True
        elif ln == "text":
            has_text = True
    if has_image:
        warnings.append(
            "embedded raster <image> elements found — these pass through to "
            "Ghostscript ICC conversion as-is; for highest fidelity, convert "
            "raster assets to CMYK separately."
        )
    if has_text:
        warnings.append(
            "<text> elements found — convert text to paths in Affinity (Layer "
            "→ Convert to Curves) before export to avoid font embedding issues."
        )
    return warnings


# --------------------------------------------------------------------------- #
# Single-file conversion
# --------------------------------------------------------------------------- #
def process_one(
    svg_path: Path,
    correction_map: dict[str, str],
    ctx: CmykContext,
) -> FileResult:
    """Run the three-stage pipeline for one SVG. Never raises — errors land on the result."""
    svg_path = Path(svg_path)
    started = time.time()
    stem = svg_path.stem
    result = FileResult(filename=svg_path.name, status="ok")

    try:
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        tmp = ctx.resolved_tmp_dir()
        tmp.mkdir(parents=True, exist_ok=True)

        result.warnings = detect_svg_warnings(svg_path)

        # 1. Apply RGB→RGB pre-correction. Even if the map is empty we still
        #    pass through to normalize hex case in the SVG; that's harmless.
        corrected_svg = tmp / f"{stem}_corrected.svg"
        body, report = apply_mapping_with_report(svg_path, correction_map)
        corrected_svg.write_bytes(body)
        result.replacements = report.replacements
        result.unmapped_colors = sorted(report.unmapped)

        # 1b. Patch the SVG root with physical inches so Inkscape produces a
        #     PDF at the correct page size (trim + bleed). Square art on a
        #     5.5×7.5 page letterboxes via xMidYMid meet.
        page_w = ctx.width_inches + 2 * ctx.bleed_inches
        page_h = ctx.height_inches + 2 * ctx.bleed_inches
        _apply_page_size(corrected_svg, page_w, page_h)

        # 2. SVG → RGB PDF.
        rgb_pdf = tmp / f"{stem}_rgb.pdf"
        svg_to_pdf(
            corrected_svg, rgb_pdf,
            width_inches=ctx.width_inches,
            height_inches=ctx.height_inches,
            bleed_inches=ctx.bleed_inches,
            inkscape_exe=ctx.inkscape_exe,
        )

        # 3. RGB PDF → CMYK PDF.
        cmyk_pdf = ctx.output_dir / f"{stem}_CMYK.pdf"
        rgb_pdf_to_cmyk(
            rgb_pdf, cmyk_pdf,
            icc_profile=ctx.icc_profile,
            gs_exe=ctx.ghostscript_exe,
            pdfx=ctx.pdfx,
        )
        result.output_pdf = cmyk_pdf

        # 4. Optional soft-proof PNG.
        if ctx.generate_preview:
            preview = ctx.output_dir / f"{stem}_CMYK_preview.png"
            try:
                pdf_to_preview_png(
                    cmyk_pdf, preview,
                    icc_profile=ctx.icc_profile,
                    dpi=ctx.preview_dpi,
                    gs_exe=ctx.ghostscript_exe,
                )
                result.preview_png = preview
            except CmykConvertError as exc:
                result.warnings.append(f"preview PNG failed: {exc}")

    except (
        FileNotFoundError,
        InkscapeNotFoundError,
        SvgToPdfError,
        GhostscriptNotFoundError,
        IccProfileNotFoundError,
        CmykConvertError,
    ) as exc:
        log.error("CMYK pipeline failed for %s: %s", svg_path.name, exc)
        result.status = "error"
        result.error = str(exc)
    except Exception as exc:  # pragma: no cover — defensive against surprises
        log.exception("Unexpected error converting %s", svg_path.name)
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.elapsed_seconds = round(time.time() - started, 3)

    return result


# --------------------------------------------------------------------------- #
# Soft-proof for the CMYK Editor (single SVG, on demand)
# --------------------------------------------------------------------------- #
def soft_proof_one(
    svg_path: Path,
    correction_map: dict[str, str],
    ctx: CmykContext,
) -> FileResult:
    """Same as :func:`process_one` but writes everything into a temp scratch dir.

    Used by the CMYK Editor's "Generate CMYK soft-proof" button. The result's
    ``output_pdf`` and ``preview_png`` paths live under ``ctx.tmp_dir`` so the
    main output folder isn't polluted.
    """
    scratch = ctx.resolved_tmp_dir() / "softproof"
    scratch.mkdir(parents=True, exist_ok=True)
    proof_ctx = CmykContext(
        output_dir=scratch,
        icc_profile=ctx.icc_profile,
        inkscape_exe=ctx.inkscape_exe,
        ghostscript_exe=ctx.ghostscript_exe,
        width_inches=ctx.width_inches,
        height_inches=ctx.height_inches,
        bleed_inches=ctx.bleed_inches,
        pdfx=False,  # soft-proof never enforces PDF/X
        generate_preview=True,
        preview_dpi=ctx.preview_dpi,
        tmp_dir=scratch / "_tmp",
    )
    return process_one(svg_path, correction_map, proof_ctx)


# --------------------------------------------------------------------------- #
# Batch
# --------------------------------------------------------------------------- #
def process_batch(
    svg_paths: list[Path],
    correction_map: dict[str, str],
    ctx: CmykContext,
    on_progress: Optional[Callable[[int, int, FileResult], None]] = None,
) -> BatchReport:
    """Run the pipeline for every SVG, collecting per-file results."""
    from datetime import datetime, timezone

    started = time.time()
    report = BatchReport(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        icc_profile=str(ctx.icc_profile),
        pdfx=ctx.pdfx,
        width_inches=ctx.width_inches,
        height_inches=ctx.height_inches,
        bleed_inches=ctx.bleed_inches,
        palette_mapped=dict(correction_map),
    )

    palette: dict[str, int] = {}
    for path in svg_paths:
        try:
            for h in parse_svg(path).colors:
                palette[h] = palette.get(h, 0) + 1
        except Exception as exc:  # pragma: no cover — palette is best-effort
            log.warning("Could not parse palette from %s: %s", path, exc)
    report.palette = palette

    total = len(svg_paths)
    for i, p in enumerate(svg_paths, start=1):
        r = process_one(p, correction_map, ctx)
        report.files.append(r)
        if on_progress is not None:
            on_progress(i, total, r)

    report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report.total_seconds = round(time.time() - started, 3)
    return report
