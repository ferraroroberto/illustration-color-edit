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
from .bleed_overlay import composite_guides
from .cmyk_tac import TacComputeError, TacReport, compute_tac
from .filename_template import TemplateError, apply_template
from .force_k import FineLineReport, find_fine_lines
from .svg_parser import _localname, parse_svg
from .svg_to_pdf import InkscapeNotFoundError, SvgToPdfError, svg_to_pdf
from .svg_writer import apply_mapping_with_report
from .trim_to_content import TrimError, TrimReport, trim_svg_to_content

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
    # Output filename template. Empty = "<stem>_CMYK.pdf" historical default.
    # See :mod:`src.filename_template` for the placeholder grammar.
    filename_template: str = ""
    # TAC and force-K knobs. The check itself always runs (cheap detection);
    # the auto-fix flags are only applied when the per-file ``cmyk_auto_fix``
    # flag is on (passed in via ``apply_auto_fix`` below).
    tac_limit_percent: float = 320.0
    tac_check_dpi: int = 100
    force_k_min_stroke_pt: float = 0.5
    force_k_min_text_pt: float = 9.0
    apply_auto_fix: bool = False
    """Per-file opt-in. When True, Ghostscript's force-K flags are added
    so exact-black text/vectors land on K only. The detection pass still
    runs in either case so the audit report stays accurate."""
    safety_inches: float = 0.1875
    show_guide_overlay: bool = True
    trim_to_content_enabled: bool = False
    """When True, the PDF page is cropped to the SVG's visible content
    (replaces the fixed trim). ``width_inches`` / ``height_inches`` /
    ``bleed_inches`` are bypassed for that file; guide overlay is
    suppressed because there are no trim/bleed/safety margins to draw."""
    trim_to_content_padding_pt: float = 0.0
    """Padding (pt) added around the trimmed bbox on all sides."""
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
    tac: Optional[TacReport] = None
    fine_lines: Optional[FineLineReport] = None
    auto_fix_applied: bool = False
    """Whether the Ghostscript force-K flags were active for this file."""
    trim: Optional[TrimReport] = None
    """Trim-to-content result. ``None`` if trim wasn't attempted (toggle off).
    ``trim.had_content == False`` means trim was attempted but the SVG had
    no visible content and the file fell back to the configured trim."""


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
    tac: Optional["TacReport"] = None,
    fine_lines: Optional["FineLineReport"] = None,
    auto_fix_applied: bool = False,
    trim: Optional["TrimReport"] = None,
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

    if tac is not None:
        tac_block = (
            f"  Threshold:        {tac.threshold_pct:.0f}%\n"
            f"  Max coverage:     {tac.max_pct:.1f}%\n"
            f"  99th percentile:  {tac.p99_pct:.1f}%\n"
            f"  Mean coverage:    {tac.mean_pct:.1f}%\n"
            f"  Pixels over limit: {tac.violation_fraction*100:.4f}%  "
            f"[{tac.status.upper()}]"
        )
    else:
        tac_block = "  (not measured)"

    if trim is not None:
        if trim.had_content:
            trim_block = (
                f"  Mode:             enabled (page = artwork extent)\n"
                f"  Original viewBox: {trim.original_viewbox or '(none)'}\n"
                f"  Trimmed viewBox:  {trim.new_viewbox}\n"
                f"  Final page:       {trim.width_in:.3f} x {trim.height_in:.3f} in\n"
                f"  Padding:          {trim.padding_pt:.2f} pt"
            )
        else:
            trim_block = (
                "  Mode:             enabled, fell back to configured trim "
                "(no visible content detected)"
            )
    else:
        trim_block = "  Mode:             disabled (using configured trim)"

    if fine_lines is not None:
        fl_lines = [f"  Auto-fix applied: {'yes' if auto_fix_applied else 'no (detection only)'}"]
        fl_lines.append(f"  Fine strokes:     {fine_lines.stroke_count}")
        fl_lines.append(f"  Small text:       {fine_lines.text_count}")
        for s in fine_lines.samples[:8]:
            fl_lines.append(f"    - {s.kind} {s.size_pt:.2f}pt  {s.color_hex}  {s.sample}")
        if fine_lines.total > len(fine_lines.samples[:8]):
            fl_lines.append(f"    ...({fine_lines.total - len(fine_lines.samples[:8])} more)")
        fl_block = "\n".join(fl_lines)
    else:
        fl_block = "  (not measured)"

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

Trim-to-content
---------------
{trim_block}

Pre-correction (RGB to RGB before Ghostscript)
----------------------------------------------
Replacements:     {replacements} total
{shifts_block}
Unmapped colors:  {unmapped_str}

Total Area Coverage (TAC)
-------------------------
{tac_block}

Force-K (fine lines / small text)
---------------------------------
{fl_block}

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

        # Resolve the output filename. Honour ctx.filename_template when set,
        # falling back to "<stem>_CMYK" when empty. A template that needs a
        # chapter prefix the source filename doesn't carry surfaces as a
        # warning rather than killing the batch.
        out_stem = f"{stem}_CMYK"
        if ctx.filename_template:
            try:
                out_stem = apply_template(ctx.filename_template, stem)
            except TemplateError as exc:
                result.warnings.append(
                    f"filename template fell back to default: {exc}"
                )

        # Sweep any audit sidecars left from a previous export of this stem.
        # Done up front so a re-run that disables audit_artifacts (or fails
        # before the report stage) leaves no orphans behind.
        cmyk_pdf = ctx.output_dir / f"{out_stem}.pdf"
        _purge_prior_sidecars(cmyk_pdf)

        result.warnings = detect_svg_warnings(svg_path)

        # Fine-line / small-text detection runs unconditionally — cheap, and
        # the audit sidecar shows it whether or not auto-fix is enabled.
        try:
            result.fine_lines = find_fine_lines(
                svg_path,
                trim_inches=(ctx.width_inches, ctx.height_inches),
                min_stroke_pt=ctx.force_k_min_stroke_pt,
                min_text_pt=ctx.force_k_min_text_pt,
            )
            if result.fine_lines.total > 0 and not ctx.apply_auto_fix:
                result.warnings.append(
                    f"force-K candidates: {result.fine_lines.summary()} — "
                    "enable per-file auto-fix or convert these to pure black "
                    "in the source to keep them on the K plate."
                )
        except Exception as exc:  # pragma: no cover — defensive
            log.warning("fine-line detection failed for %s: %s", svg_path.name, exc)

        # 1. Apply RGB→RGB pre-correction. Even if the map is empty we still
        #    pass through to normalize hex case in the SVG; that's harmless.
        corrected_svg = tmp / f"{stem}_corrected.svg"
        body, report = apply_mapping_with_report(svg_path, correction_map)
        corrected_svg.write_bytes(body)
        result.replacements = report.replacements
        result.replacements_by_source = dict(report.by_source)
        result.unmapped_colors = sorted(report.unmapped)

        # 1b. Either trim the SVG to its visible content (overrides the
        #     configured trim) or letterbox it inside the configured trim.
        #
        # The trim path bypasses ``_apply_page_size``: ``trim_svg_to_content``
        # already wrote width/height in inches matching the artwork bbox,
        # which is exactly what Inkscape needs to produce a page-sized-to-
        # content PDF. Bleed is forced to 0 here because the publisher use
        # case is "page = artwork extent"; the guide overlay is suppressed
        # downstream for the same reason (no trim/bleed/safety to draw).
        use_trim = False
        page_w_in = ctx.width_inches
        page_h_in = ctx.height_inches
        bleed_in = ctx.bleed_inches
        if ctx.trim_to_content_enabled:
            try:
                trim_report = trim_svg_to_content(
                    corrected_svg, corrected_svg,
                    padding_pt=ctx.trim_to_content_padding_pt,
                    inkscape_exe=ctx.inkscape_exe,
                )
            except TrimError as exc:
                # Inkscape bbox query failed — surface as a per-file warning
                # and fall through to the configured trim so the batch keeps
                # going on the remaining illustrations.
                log.warning("trim-to-content failed for %s: %s", svg_path.name, exc)
                result.warnings.append(f"trim-to-content failed: {exc}")
                trim_report = None
            result.trim = trim_report
            if trim_report and trim_report.had_content:
                use_trim = True
                page_w_in = trim_report.width_in
                page_h_in = trim_report.height_in
                bleed_in = 0.0
            elif trim_report is not None:
                result.warnings.append(
                    "trim-to-content: no visible content detected — "
                    "fell back to configured trim size."
                )
        if not use_trim:
            page_w = ctx.width_inches + 2 * ctx.bleed_inches
            page_h = ctx.height_inches + 2 * ctx.bleed_inches
            _apply_page_size(corrected_svg, page_w, page_h)

        # 2. SVG → RGB PDF.
        rgb_pdf = tmp / f"{stem}_rgb.pdf"
        svg_to_pdf(
            corrected_svg, rgb_pdf,
            width_inches=page_w_in,
            height_inches=page_h_in,
            bleed_inches=bleed_in,
            inkscape_exe=ctx.inkscape_exe,
        )

        # 3. RGB PDF → CMYK PDF.
        rgb_pdf_to_cmyk(
            rgb_pdf, cmyk_pdf,
            icc_profile=ctx.icc_profile,
            gs_exe=ctx.ghostscript_exe,
            pdfx=ctx.pdfx,
            keep_pdfx_def_ps=ctx.audit_artifacts,
            force_k=ctx.apply_auto_fix,
        )
        result.output_pdf = cmyk_pdf
        result.auto_fix_applied = ctx.apply_auto_fix

        # 3b. TAC check on the produced CMYK PDF. Best-effort: a failure
        #     in the rasterizer should not kill the conversion result.
        try:
            result.tac = compute_tac(
                cmyk_pdf,
                gs_exe=ctx.ghostscript_exe,
                dpi=ctx.tac_check_dpi,
                threshold_pct=ctx.tac_limit_percent,
            )
            if result.tac.status == "fail":
                result.warnings.append(
                    f"TAC: {result.tac.violation_fraction*100:.2f}% of pixels "
                    f"exceed {ctx.tac_limit_percent:.0f}% (max {result.tac.max_pct:.0f}%) "
                    "— printer may reject; consider less-saturated targets in "
                    "the CMYK correction map."
                )
            elif result.tac.status == "warn":
                result.warnings.append(
                    f"TAC: a few pixels (<0.1%) exceed {ctx.tac_limit_percent:.0f}% "
                    f"(max {result.tac.max_pct:.0f}%) — usually safe."
                )
        except (TacComputeError, GhostscriptNotFoundError) as exc:
            log.warning("TAC check failed for %s: %s", svg_path.name, exc)
            result.warnings.append(f"TAC check unavailable: {exc}")

        # 4. Optional soft-proof PNG.
        if ctx.generate_preview:
            preview = ctx.output_dir / f"{out_stem}_preview.png"
            try:
                pdf_to_preview_png(
                    cmyk_pdf, preview,
                    icc_profile=ctx.icc_profile,
                    dpi=ctx.preview_dpi,
                    gs_exe=ctx.ghostscript_exe,
                )
                result.preview_png = preview
                # Skip guides when trim-to-content is active: there's no
                # trim/bleed/safety to mark — the page IS the artwork.
                if ctx.show_guide_overlay and not use_trim:
                    try:
                        composite_guides(
                            preview,
                            trim_w_in=ctx.width_inches,
                            trim_h_in=ctx.height_inches,
                            bleed_in=ctx.bleed_inches,
                            safety_in=ctx.safety_inches,
                            dpi=ctx.preview_dpi,
                        )
                    except Exception as gxc:  # pragma: no cover — defensive
                        log.warning("guide overlay failed for %s: %s",
                                    preview.name, gxc)
                        result.warnings.append(f"guide overlay failed: {gxc}")
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
                force_k=ctx.apply_auto_fix,
            )
            write_conversion_report(
                report_path=report_path,
                svg_path=svg_path,
                cmyk_pdf=cmyk_pdf,
                preview_png=result.preview_png,
                pdfx_def_ps=pdfx_def_ps if (ctx.pdfx and pdfx_def_ps.is_file()) else None,
                icc_profile=ctx.icc_profile,
                pdfx=ctx.pdfx,
                width_inches=page_w_in,
                height_inches=page_h_in,
                bleed_inches=bleed_in,
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
                tac=result.tac,
                fine_lines=result.fine_lines,
                auto_fix_applied=result.auto_fix_applied,
                trim=result.trim,
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
        trim_to_content_enabled=ctx.trim_to_content_enabled,
        trim_to_content_padding_pt=ctx.trim_to_content_padding_pt,
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
