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
    _output_condition_for_profile,
    _resolve_ghostscript,
    build_gs_command,
    get_ghostscript_version,
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
    audit_artifacts: bool = True
    tmp_dir: Optional[Path] = None  # defaults to output_dir / "_tmp"
    # Cached at batch start so each file's report can record it without
    # spawning N extra `gs -v` subprocesses. Empty string = not yet probed.
    ghostscript_version: str = ""

    def resolved_tmp_dir(self) -> Path:
        return self.tmp_dir or (self.output_dir / "_tmp")


@dataclass
class FileResult:
    """Per-file outcome row in the batch report."""

    filename: str
    status: str  # "ok" | "error"
    output_pdf: Optional[Path] = None
    preview_png: Optional[Path] = None
    report_txt: Optional[Path] = None
    replacements: int = 0
    # Per-source-hex breakdown of pre-correction replacements (source -> count).
    # Captured so the audit report can list each #SRC → #TGT shift individually.
    replacements_by_source: dict[str, int] = field(default_factory=dict)
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
# Audit sidecars (orphan cleanup + per-file report)
# --------------------------------------------------------------------------- #
def _sidecar_paths(cmyk_pdf: Path) -> tuple[Path, Path]:
    """Return (pdfx_def_ps, report_txt) for a given output PDF."""
    return (
        cmyk_pdf.with_suffix(".pdfx_def.ps"),
        cmyk_pdf.parent / f"{cmyk_pdf.stem}_report.txt",
    )


def _purge_prior_sidecars(cmyk_pdf: Path) -> None:
    """Remove leftover audit files for this output stem before a fresh run.

    Keeps the output folder consistent with the *current* settings: if the
    user previously exported with ``audit_artifacts`` on and now turns it
    off, the next run sweeps away the obsolete companions instead of
    leaving them as orphans.
    """
    for path in _sidecar_paths(cmyk_pdf):
        path.unlink(missing_ok=True)


def _format_bytes(n: int) -> str:
    if n >= 1_048_576:
        return f"{n/1_048_576:.2f} MB"
    if n >= 1024:
        return f"{n/1024:.1f} KB"
    return f"{n} B"


def _safe_size(path: Optional[Path]) -> str:
    if path is None or not path.is_file():
        return "—"
    return _format_bytes(path.stat().st_size)


def write_conversion_report(
    *,
    report_path: Path,
    svg_path: Path,
    cmyk_pdf: Path,
    preview_png: Optional[Path],
    pdfx_def_ps: Optional[Path],
    icc_profile: Path,
    pdfx: bool,
    width_inches: float,
    height_inches: float,
    bleed_inches: float,
    replacements: int,
    replacements_by_source: dict[str, int],
    correction_map: dict[str, str],
    unmapped_colors: list[str],
    warnings: list[str],
    inkscape_exe: str,
    gs_resolved_path: str,
    gs_version: str,
    gs_command: list[str],
    elapsed_seconds: float,
    started_iso: str,
) -> Path:
    """Write a human-readable audit report for one CMYK conversion.

    Intended for the book editor / prepress operator: every value is the
    actual one used by Ghostscript and Inkscape on this run, not a copy of
    the configuration. If the editor needs to reproduce the file from
    scratch, the embedded ICC, OutputCondition, page geometry and the full
    GS command line give them everything they need.
    """
    cond_id, cond_label = _output_condition_for_profile(icc_profile)
    page_w = width_inches + 2 * bleed_inches
    page_h = height_inches + 2 * bleed_inches
    pdfx_label = "PDF/X-1a:2003" if pdfx else "plain DeviceCMYK"
    unmapped_str = ", ".join(unmapped_colors) if unmapped_colors else "(none)"
    warnings_block = (
        "\n".join(f"  - {w}" for w in warnings) if warnings else "  (none)"
    )
    # Per-color CMYK pre-shift table — sorted by occurrence count desc so
    # the most-impacted colors land at the top. ``correction_map`` keys are
    # uppercased on load; ``by_source`` keys come from the SVG so we
    # uppercase on lookup to be safe.
    if replacements_by_source:
        upper_map = {k.upper(): v for k, v in correction_map.items()}
        shift_lines = []
        for src, count in sorted(
            replacements_by_source.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            tgt = upper_map.get(src.upper(), "?")
            shift_lines.append(f"  {src} -> {tgt}  ({count} {'token' if count == 1 else 'tokens'})")
        shifts_block = "\n".join(shift_lines)
    else:
        shifts_block = "  (none)"
    pdfx_def_line = (
        f"{pdfx_def_ps.name} ({_safe_size(pdfx_def_ps)})" if pdfx_def_ps else "not generated"
    )
    preview_line = (
        f"{preview_png.name} ({_safe_size(preview_png)})" if preview_png else "not generated"
    )

    body = f"""CMYK conversion report
======================
Generated:        {started_iso}
Source SVG:       {svg_path.name} ({_safe_size(svg_path)})
Output PDF:       {cmyk_pdf.name} ({_safe_size(cmyk_pdf)})
Soft-proof PNG:   {preview_line}
PDF/X def file:   {pdfx_def_line}

Color management
----------------
ICC profile:      {icc_profile.name} ({_safe_size(icc_profile)})
Profile path:     {icc_profile}
Output condition: {cond_id} — {cond_label}
PDF/X compliance: {pdfx_label}

Page geometry
-------------
Trim:             {width_inches:.3f} x {height_inches:.3f} in
Bleed:            {bleed_inches:.3f} in (each side)
PDF MediaBox:     {page_w:.3f} x {page_h:.3f} in

Pre-correction (RGB to RGB before Ghostscript)
----------------------------------------------
Replacements:     {replacements} total
{shifts_block}
Unmapped colors:  {unmapped_str}

SVG content warnings
--------------------
{warnings_block}

Tooling
-------
Ghostscript:      {gs_version}
Ghostscript path: {gs_resolved_path}
Inkscape path:    {inkscape_exe}
Elapsed:          {elapsed_seconds:.3f} s

Ghostscript command
-------------------
{_format_command(gs_command)}
"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(body, encoding="utf-8")
    return report_path


def _format_command(cmd: list[str]) -> str:
    """Format an argv list for human inspection: one arg per line, quoted if needed."""
    lines = []
    for i, arg in enumerate(cmd):
        # Quote arguments that contain whitespace so the editor can copy a
        # line into a shell. Internal quotes are doubled (PowerShell style)
        # rather than backslash-escaped because the user is on Windows.
        needs_quote = any(c.isspace() for c in arg)
        rendered = f'"{arg}"' if needs_quote else arg
        prefix = "  " if i > 0 else ""
        lines.append(f"{prefix}{rendered}")
    return " \\\n".join(lines)


# --------------------------------------------------------------------------- #
# Single-file conversion
# --------------------------------------------------------------------------- #
def process_one(
    svg_path: Path,
    correction_map: dict[str, str],
    ctx: CmykContext,
) -> FileResult:
    """Run the three-stage pipeline for one SVG. Never raises — errors land on the result."""
    from datetime import datetime, timezone

    svg_path = Path(svg_path)
    started = time.time()
    started_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stem = svg_path.stem
    result = FileResult(filename=svg_path.name, status="ok")

    try:
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        tmp = ctx.resolved_tmp_dir()
        tmp.mkdir(parents=True, exist_ok=True)

        # Sweep any audit sidecars left from a previous export of this stem.
        # Done up front so a re-run that disables audit_artifacts (or fails
        # before the report stage) leaves no orphans behind.
        cmyk_pdf = ctx.output_dir / f"{stem}_CMYK.pdf"
        _purge_prior_sidecars(cmyk_pdf)

        result.warnings = detect_svg_warnings(svg_path)

        # 1. Apply RGB→RGB pre-correction. Even if the map is empty we still
        #    pass through to normalize hex case in the SVG; that's harmless.
        corrected_svg = tmp / f"{stem}_corrected.svg"
        body, report = apply_mapping_with_report(svg_path, correction_map)
        corrected_svg.write_bytes(body)
        result.replacements = report.replacements
        result.replacements_by_source = dict(report.by_source)
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
        rgb_pdf_to_cmyk(
            rgb_pdf, cmyk_pdf,
            icc_profile=ctx.icc_profile,
            gs_exe=ctx.ghostscript_exe,
            pdfx=ctx.pdfx,
            keep_pdfx_def_ps=ctx.audit_artifacts,
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

        # 5. Optional audit report. Written last so it can record the final
        #    sizes (including the soft-proof PNG, if any).
        if ctx.audit_artifacts:
            pdfx_def_ps, report_path = _sidecar_paths(cmyk_pdf)
            try:
                gs_resolved = _resolve_ghostscript(ctx.ghostscript_exe)
            except GhostscriptNotFoundError:
                gs_resolved = ctx.ghostscript_exe
            gs_command = build_gs_command(
                rgb_pdf, cmyk_pdf, ctx.icc_profile, gs_resolved,
                pdfx=ctx.pdfx,
                pdfx_def_ps=pdfx_def_ps if ctx.pdfx else None,
            )
            write_conversion_report(
                report_path=report_path,
                svg_path=svg_path,
                cmyk_pdf=cmyk_pdf,
                preview_png=result.preview_png,
                pdfx_def_ps=pdfx_def_ps if (ctx.pdfx and pdfx_def_ps.is_file()) else None,
                icc_profile=ctx.icc_profile,
                pdfx=ctx.pdfx,
                width_inches=ctx.width_inches,
                height_inches=ctx.height_inches,
                bleed_inches=ctx.bleed_inches,
                replacements=result.replacements,
                replacements_by_source=result.replacements_by_source,
                correction_map=correction_map,
                unmapped_colors=result.unmapped_colors,
                warnings=result.warnings,
                inkscape_exe=ctx.inkscape_exe,
                gs_resolved_path=gs_resolved,
                gs_version=ctx.ghostscript_version or "unknown",
                gs_command=gs_command,
                elapsed_seconds=round(time.time() - started, 3),
                started_iso=started_iso,
            )
            result.report_txt = report_path

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
        # Soft-proofs are throwaway previews — no audit sidecars.
        audit_artifacts=False,
        tmp_dir=scratch / "_tmp",
        ghostscript_version=ctx.ghostscript_version,
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
    # Probe Ghostscript once for the whole batch so each per-file report can
    # cite the version without spawning N extra subprocesses.
    if not ctx.ghostscript_version:
        ctx.ghostscript_version = get_ghostscript_version(ctx.ghostscript_exe)
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
