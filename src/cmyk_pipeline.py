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
from dataclasses import dataclass, field, replace
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
    pdfx_mode_label,
    pdf_to_preview_png,
    rgb_pdf_to_cmyk,
)
from .bleed_overlay import composite_guides
from .cmyk_tac import TacComputeError, TacReport, compute_tac
from .config import AppConfig
from .device_cmyk import (
    DeviceCmykError,
    DeviceCmyk,
    DeviceCmykPatchReport,
    normalize_device_cmyk_overrides,
    patch_pdf_device_cmyk_values_to_exact,
    patch_pdf_rgb_colors_to_device_cmyk,
)
from .filename_template import TemplateError, apply_template
from .force_k import FineLineReport, find_fine_lines
from .mapping_store import MappingStore
from .render_check import RenderCheckError, check_render_fidelity
from .semantic_palette import SemanticPalette, merge_with_semantic
from .svg_parser import _localname, parse_svg
from .svg_to_pdf import InkscapeNotFoundError, SvgToPdfError, svg_to_pdf
from .svg_writer import apply_mapping_with_report
from .trim_to_content import TrimError, TrimReport, trim_svg_to_content
from .utils import format_bytes

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
    pdfx: bool | str = False
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
    print_dir: Optional[Path] = None
    """Where the print deliverables land (PDF, audit sidecars, the trimmed
    ``_preview_cut.png``, QA report). ``None`` falls back to ``output_dir``
    so tests and older callers keep flat-layout behavior."""
    preview_dir: Optional[Path] = None
    """Where the full uncropped ``_preview_full.png`` lands. ``None`` falls
    back to the resolved ``print_dir`` (i.e. flat layout)."""
    generate_full_preview: bool = False
    """When True, also render a second soft-proof PNG at the SVG's natural
    aspect (no trim, no letterbox) into ``preview_dir``. Off by default in
    the dataclass to keep test construction terse; ``config.json`` defaults
    it on for the real pipeline."""
    render_check_enabled: bool = False
    """When True, diff the SVG's Inkscape render against the RGB PDF render
    to catch Inkscape PDF-export shape-dropping (issue #8) and surface it as
    a per-file warning. Adds one extra Inkscape + Ghostscript render per
    file. Off by default in the dataclass (tests stay fast); ``config.json``
    defaults it on for the real pipeline."""
    render_check_dpi: int = 300
    """Resolution for the render-fidelity diff. 300 dpi resolves a dropped
    emoji-dot-sized shape comfortably above rasteriser anti-alias noise."""
    # Cached at batch start so each file's report can record it without
    # spawning N extra `gs -v` subprocesses. Empty string = not yet probed.
    ghostscript_version: str = ""

    def resolved_tmp_dir(self) -> Path:
        return self.tmp_dir or (self.output_dir / "_tmp")

    def resolved_print_dir(self) -> Path:
        """Effective print-output directory, falling back to ``output_dir``."""
        return self.print_dir or self.output_dir

    def resolved_preview_dir(self) -> Path:
        """Effective full-preview directory, falling back to print_dir."""
        return self.preview_dir or self.resolved_print_dir()


@dataclass
class FileResult:
    """Per-file outcome row in the batch report."""

    filename: str
    status: str  # "ok" | "error"
    output_pdf: Optional[Path] = None
    preview_png: Optional[Path] = None
    """The PDF-matching soft-proof (renamed to ``_preview_cut.png`` on disk).
    Kept named ``preview_png`` on the dataclass so existing call sites and
    tests keep working."""
    preview_full_png: Optional[Path] = None
    """Optional full-aspect uncropped soft-proof, written into
    ``ctx.resolved_preview_dir()``. ``None`` when full-preview is disabled
    or its render failed."""
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
    device_cmyk: Optional[DeviceCmykPatchReport] = None
    """Exact DeviceCMYK override patch report."""
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
    pdfx: bool | str = False
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


def _read_viewbox_aspect(svg_path: Path) -> Optional[tuple[float, float]]:
    """Return ``(width_units, height_units)`` from the SVG viewBox, or ``None``.

    Used by :func:`_render_full_preview` to pick a page size that preserves
    the artwork's natural aspect ratio. Falls back to ``None`` when the
    viewBox is missing or unparseable — the caller then uses a fallback.
    """
    try:
        tree = etree.parse(str(svg_path))
    except (OSError, etree.XMLSyntaxError):
        return None
    vb = (tree.getroot().get("viewBox") or "").strip()
    if not vb:
        return None
    parts = vb.replace(",", " ").split()
    if len(parts) != 4:
        return None
    try:
        _, _, w, h = (float(p) for p in parts)
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return w, h


def _render_full_preview(
    *,
    corrected_full_svg: Path,
    out_png: Path,
    tmp_dir: Path,
    stem: str,
    ctx: "CmykContext",
    device_cmyk_overrides: Optional[dict[str, DeviceCmyk]] = None,
) -> None:
    """Render the corrected SVG to a CMYK soft-proof PNG at natural aspect.

    Sizes the page so the longer side matches the larger of the configured
    trim dimensions, preserving the SVG viewBox aspect. The intermediate
    RGB and CMYK PDFs are throwaway and live under ``tmp_dir``; only the
    PNG persists at ``out_png``.

    Re-raises :class:`CmykConvertError` / :class:`SvgToPdfError` so the
    caller can record a per-file warning without killing the batch.
    """
    aspect = _read_viewbox_aspect(corrected_full_svg)
    longest_in = max(ctx.width_inches, ctx.height_inches)
    if aspect is None:
        # Fall back to the configured trim — better than guessing.
        full_w_in = ctx.width_inches
        full_h_in = ctx.height_inches
    else:
        vb_w, vb_h = aspect
        if vb_w >= vb_h:
            full_w_in = longest_in
            full_h_in = longest_in * (vb_h / vb_w)
        else:
            full_h_in = longest_in
            full_w_in = longest_in * (vb_w / vb_h)

    _apply_page_size(corrected_full_svg, full_w_in, full_h_in)

    full_rgb_pdf = tmp_dir / f"{stem}_rgb_full.pdf"
    svg_to_pdf(
        corrected_full_svg, full_rgb_pdf,
        width_inches=full_w_in,
        height_inches=full_h_in,
        bleed_inches=0.0,
        inkscape_exe=ctx.inkscape_exe,
    )
    if device_cmyk_overrides:
        patch_pdf_rgb_colors_to_device_cmyk(full_rgb_pdf, device_cmyk_overrides)
    full_cmyk_pdf = tmp_dir / f"{stem}_full_CMYK.pdf"
    rgb_pdf_to_cmyk(
        full_rgb_pdf, full_cmyk_pdf,
        icc_profile=ctx.icc_profile,
        gs_exe=ctx.ghostscript_exe,
        pdfx=False,  # client preview never enforces PDF/X
        keep_pdfx_def_ps=False,
        force_k=ctx.apply_auto_fix,
    )
    pdf_to_preview_png(
        full_cmyk_pdf, out_png,
        icc_profile=ctx.icc_profile,
        dpi=ctx.preview_dpi,
        gs_exe=ctx.ghostscript_exe,
    )


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


def _preview_paths(print_dir: Path, preview_dir: Path, out_stem: str) -> tuple[Path, Path]:
    """Return ``(cut_preview, full_preview)`` PNG paths for an output stem."""
    return (
        print_dir / f"{out_stem}_preview_cut.png",
        preview_dir / f"{out_stem}_preview_full.png",
    )


def _purge_prior_previews(print_dir: Path, preview_dir: Path, out_stem: str) -> None:
    """Remove leftover soft-proof PNGs for this stem before a fresh export.

    Mirrors :func:`_purge_prior_sidecars`: a re-run that turns
    ``generate_preview`` / ``generate_full_preview`` off (or fails before the
    preview stage) must not leave a stale ``_preview_cut.png`` /
    ``_preview_full.png`` behind — otherwise the previous run's preview
    lingers and could be handed to the client as if it were current. The
    enabled previews are regenerated immediately after, so a normal re-run
    just refreshes them.
    """
    for path in _preview_paths(print_dir, preview_dir, out_stem):
        path.unlink(missing_ok=True)


def _safe_size(path: Optional[Path]) -> str:
    if path is None or not path.is_file():
        return "—"
    return format_bytes(path.stat().st_size)


def write_conversion_report(
    *,
    report_path: Path,
    svg_path: Path,
    cmyk_pdf: Path,
    preview_png: Optional[Path],
    pdfx_def_ps: Optional[Path],
    icc_profile: Path,
    pdfx: bool | str,
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
    device_cmyk: Optional["DeviceCmykPatchReport"] = None,
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
    pdfx_label = pdfx_mode_label(pdfx)
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

    if device_cmyk is not None and device_cmyk.requested:
        device_lines = [
            f"  Requested colors: {device_cmyk.requested}",
            f"  RGB PDF operators patched:   {device_cmyk.operators_rewritten}",
            f"  RGB PDF streams patched:     {device_cmyk.streams_rewritten}",
            f"  Final PDF operators snapped: {device_cmyk.final_operators_rewritten}",
            f"  Final PDF streams snapped:   {device_cmyk.final_streams_rewritten}",
        ]
        for src, count in sorted(device_cmyk.by_source.items()):
            device_lines.append(f"  {src}: {count} operator{'s' if count != 1 else ''}")
        if device_cmyk.missing_sources:
            device_lines.append(
                "  Missing in RGB PDF: " + ", ".join(device_cmyk.missing_sources)
            )
        device_block = "\n".join(device_lines)
    else:
        device_block = "  (none)"

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

Exact DeviceCMYK overrides
--------------------------
{device_block}

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
    device_cmyk_overrides: Optional[dict[str, DeviceCmyk]] = None,
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
        print_out = ctx.resolved_print_dir()
        preview_out = ctx.resolved_preview_dir()
        print_out.mkdir(parents=True, exist_ok=True)
        preview_out.mkdir(parents=True, exist_ok=True)
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
        cmyk_pdf = print_out / f"{out_stem}.pdf"
        cut_preview_path, full_preview_path = _preview_paths(print_out, preview_out, out_stem)
        _purge_prior_sidecars(cmyk_pdf)
        # Also sweep stale soft-proof PNGs for this stem so a re-export refreshes
        # them (and turning a preview off doesn't leave an out-of-date orphan).
        _purge_prior_previews(print_out, preview_out, out_stem)

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

        device_cmyk_overrides = normalize_device_cmyk_overrides(
            device_cmyk_overrides or {}
        )

        # 1. Apply RGB→RGB pre-correction. Exact DeviceCMYK sources are left
        #    as their original RGB in the intermediate SVG so we can find and
        #    replace those RGB paint operators in the rendered PDF.
        corrected_svg = tmp / f"{stem}_corrected.svg"
        effective_correction_map = {
            k: v for k, v in correction_map.items()
            if k.upper() not in device_cmyk_overrides
        }
        body, report = apply_mapping_with_report(svg_path, effective_correction_map)
        corrected_svg.write_bytes(body)
        result.replacements = report.replacements
        result.replacements_by_source = dict(report.by_source)
        result.unmapped_colors = sorted(report.unmapped)

        # 1a. Snapshot the corrected SVG before trim mutates it — the full
        # preview renders from this so the client sees the artwork at its
        # natural aspect (no crop, no letterbox). Only needed when the
        # full-preview pass will run.
        corrected_full_svg: Optional[Path] = None
        if ctx.generate_full_preview:
            corrected_full_svg = tmp / f"{stem}_corrected_full.svg"
            corrected_full_svg.write_bytes(body)

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

        # 2a. Exact DeviceCMYK overrides. Patching before Ghostscript means
        #    the final PDF carries explicit DeviceCMYK paint operators for
        #    those colors while the rest of the document remains ICC-managed.
        if device_cmyk_overrides:
            result.device_cmyk = patch_pdf_rgb_colors_to_device_cmyk(
                rgb_pdf,
                device_cmyk_overrides,
            )
            if result.device_cmyk.missing_sources:
                result.warnings.append(
                    "DeviceCMYK override source color not found in RGB PDF: "
                    + ", ".join(result.device_cmyk.missing_sources)
                )

        # 2b. Render-fidelity check. Inkscape's vector PDF backend can drop a
        #     shape that its own raster render (and browsers) draw correctly —
        #     see issue #8. Diff the page-sized SVG render against the RGB PDF
        #     render and warn so the artwork can be reworked in Affinity. Both
        #     sides are RGB (pre-CMYK) so only structural differences show, not
        #     the expected ICC colour shift. Best-effort: a render failure here
        #     must not sink the conversion.
        if ctx.render_check_enabled:
            try:
                rc = check_render_fidelity(
                    corrected_svg, rgb_pdf,
                    inkscape_exe=ctx.inkscape_exe,
                    gs_exe=ctx.ghostscript_exe,
                    dpi=ctx.render_check_dpi,
                )
                if rc.has_discrepancy:
                    result.warnings.append(f"render check: {rc.summary()}")
            except RenderCheckError as exc:
                log.warning("render check unavailable for %s: %s", svg_path.name, exc)
                result.warnings.append(f"render check unavailable: {exc}")

        # 3. RGB PDF → CMYK PDF.
        rgb_pdf_to_cmyk(
            rgb_pdf, cmyk_pdf,
            icc_profile=ctx.icc_profile,
            gs_exe=ctx.ghostscript_exe,
            pdfx=ctx.pdfx,
            keep_pdfx_def_ps=ctx.audit_artifacts,
            force_k=ctx.apply_auto_fix,
        )
        if device_cmyk_overrides:
            final_patch = patch_pdf_device_cmyk_values_to_exact(
                cmyk_pdf,
                device_cmyk_overrides,
            )
            if result.device_cmyk is None:
                result.device_cmyk = final_patch
            else:
                result.device_cmyk.final_operators_rewritten = (
                    final_patch.final_operators_rewritten
                )
                result.device_cmyk.final_streams_rewritten = (
                    final_patch.final_streams_rewritten
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

        # 4. Optional soft-proof PNG — the "cut" preview that matches the
        #    PDF (trimmed when trim-to-content is on). Renamed from the
        #    historical `_preview.png` so the new full preview can sit
        #    alongside with a clear naming convention.
        if ctx.generate_preview:
            preview = cut_preview_path
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

        # 4b. Optional FULL preview — the SVG at its natural aspect (no trim,
        #     no letterbox), soft-proofed through the same ICC pipeline.
        #     Lands in ``preview_dir`` so the client deliverables form a
        #     scannable folder distinct from the print stream.
        if (
            ctx.generate_preview
            and ctx.generate_full_preview
            and corrected_full_svg is not None
        ):
            full_preview = full_preview_path
            try:
                _render_full_preview(
                    corrected_full_svg=corrected_full_svg,
                    out_png=full_preview,
                    tmp_dir=tmp,
                    stem=stem,
                    ctx=ctx,
                    device_cmyk_overrides=device_cmyk_overrides,
                )
                result.preview_full_png = full_preview
            except (CmykConvertError, SvgToPdfError) as exc:
                log.warning("full preview failed for %s: %s", svg_path.name, exc)
                result.warnings.append(f"full preview failed: {exc}")

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
                device_cmyk=result.device_cmyk,
            )
            result.report_txt = report_path

    except (
        FileNotFoundError,
        InkscapeNotFoundError,
        SvgToPdfError,
        GhostscriptNotFoundError,
        IccProfileNotFoundError,
        CmykConvertError,
        DeviceCmykError,
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
    device_cmyk_overrides: Optional[dict[str, DeviceCmyk]] = None,
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
        # Soft-proofs are throwaway previews — no audit sidecars and no
        # split layout. Everything goes flat into the scratch dir.
        audit_artifacts=False,
        tmp_dir=scratch / "_tmp",
        ghostscript_version=ctx.ghostscript_version,
        trim_to_content_enabled=ctx.trim_to_content_enabled,
        trim_to_content_padding_pt=ctx.trim_to_content_padding_pt,
        generate_full_preview=False,
    )
    return process_one(svg_path, correction_map, proof_ctx, device_cmyk_overrides)


# --------------------------------------------------------------------------- #
# Batch
# --------------------------------------------------------------------------- #
@dataclass
class BatchFilePlan:
    """Per-file inputs resolved by the caller for one batch entry.

    Lets :func:`process_batch` stay the single orchestration entry point while
    each caller threads in the file-specific bits it owns — the merged
    correction map, an optional per-file :class:`CmykContext` (e.g. with
    ``apply_auto_fix`` toggled), DeviceCMYK overrides, and an ``on_success``
    hook (e.g. mark the illustration "exported") run only when the file
    converts cleanly. Returned from the ``plan_file`` callback.
    """

    correction_map: dict[str, str]
    ctx: CmykContext
    device_cmyk_overrides: Optional[dict[str, DeviceCmyk]] = None
    on_success: Optional[Callable[[FileResult], None]] = None


def process_batch(
    svg_paths: list[Path],
    correction_map: dict[str, str],
    ctx: CmykContext,
    on_progress: Optional[Callable[[int, int, FileResult], None]] = None,
    device_cmyk_overrides: Optional[dict[str, DeviceCmyk]] = None,
    *,
    plan_file: Optional[Callable[[Path], BatchFilePlan]] = None,
    palette_mapped: Optional[dict[str, str]] = None,
) -> BatchReport:
    """Run the pipeline for every SVG, collecting per-file results.

    By default every file is converted with the shared ``correction_map`` /
    ``ctx`` / ``device_cmyk_overrides``. Callers that need per-file resolution
    (merged overrides, per-file auto-fix, "mark exported" on success) pass a
    ``plan_file`` callback returning a :class:`BatchFilePlan` for each path; the
    shared trio is then ignored in favour of the plan's. ``palette_mapped``
    overrides the reported source→target map (defaults to ``correction_map``).
    """
    from datetime import datetime, timezone

    started = time.time()
    # Probe Ghostscript once for the whole batch so each per-file report can
    # cite the version without spawning N extra subprocesses. Probing the
    # shared ``ctx`` up front means per-file copies (``dataclasses.replace``)
    # built by ``plan_file`` inherit the resolved version.
    if not ctx.ghostscript_version:
        ctx.ghostscript_version = get_ghostscript_version(ctx.ghostscript_exe)
    report = BatchReport(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        icc_profile=str(ctx.icc_profile),
        pdfx=ctx.pdfx,
        width_inches=ctx.width_inches,
        height_inches=ctx.height_inches,
        bleed_inches=ctx.bleed_inches,
        palette_mapped=(
            dict(palette_mapped) if palette_mapped is not None
            else dict(correction_map)
        ),
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
        if plan_file is not None:
            plan = plan_file(p)
            r = process_one(
                p, plan.correction_map, plan.ctx, plan.device_cmyk_overrides,
            )
            report.files.append(r)
            if plan.on_success is not None and r.status == "ok":
                plan.on_success(r)
        else:
            r = process_one(p, correction_map, ctx, device_cmyk_overrides)
            report.files.append(r)
        if on_progress is not None:
            on_progress(i, total, r)

    report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report.total_seconds = round(time.time() - started, 3)
    return report


# --------------------------------------------------------------------------- #
# Shared batch-build helpers (CLI ``cmd_cmyk_convert`` + CMYK Print Export tab)
# --------------------------------------------------------------------------- #
def build_cmyk_context(
    cfg: AppConfig,
    *,
    filename_template: Optional[str] = None,
    trim_to_content_enabled: Optional[bool] = None,
    trim_to_content_padding_pt: Optional[float] = None,
) -> CmykContext:
    """Build a :class:`CmykContext` from ``cfg.cmyk_export`` + ``cfg.png_export``.

    The three keyword overrides let a caller supersede the configured value
    for one run — e.g. the CLI's ``--filename-template`` / ``--trim`` /
    ``--no-trim`` / ``--trim-padding-pt`` flags — without mutating ``cfg``.
    ``None`` (the default) means "use the configured value verbatim", which is
    what the CMYK Print Export tab wants since it has no such per-run
    overrides.
    """
    ce = cfg.cmyk_export
    return CmykContext(
        output_dir=ce.output_dir,
        icc_profile=ce.icc_profile_path,
        inkscape_exe=cfg.png_export.inkscape_path,
        ghostscript_exe=ce.ghostscript_path,
        width_inches=ce.target_width_inches,
        height_inches=ce.target_height_inches,
        bleed_inches=ce.bleed_inches,
        pdfx=ce.pdfx_compliance,
        generate_preview=ce.generate_preview_png,
        preview_dpi=ce.preview_dpi,
        audit_artifacts=ce.audit_artifacts,
        filename_template=(
            ce.filename_template if filename_template is None else filename_template
        ),
        tac_limit_percent=ce.tac_limit_percent,
        tac_check_dpi=ce.tac_check_dpi,
        force_k_min_stroke_pt=ce.force_k_min_stroke_pt,
        force_k_min_text_pt=ce.force_k_min_text_pt,
        safety_inches=ce.safety_inches,
        show_guide_overlay=ce.show_guide_overlay,
        trim_to_content_enabled=(
            ce.trim_to_content_enabled if trim_to_content_enabled is None
            else trim_to_content_enabled
        ),
        trim_to_content_padding_pt=(
            ce.trim_to_content_padding_pt if trim_to_content_padding_pt is None
            else trim_to_content_padding_pt
        ),
        print_dir=ce.print_dir,
        preview_dir=ce.preview_dir,
        generate_full_preview=ce.generate_full_preview,
        render_check_enabled=ce.render_check,
        render_check_dpi=ce.render_check_dpi,
    )


def build_batch_plan_factory(
    store: MappingStore,
    cmyk_global: dict[str, dict[str, str]],
    cmyk_device_global: dict[str, DeviceCmyk],
    ctx: CmykContext,
    sem: Optional[SemanticPalette],
) -> Callable[[Path], BatchFilePlan]:
    """Return a ``plan_file`` callback for :func:`process_batch`.

    Shared by the CLI's ``cmd_cmyk_convert`` and the CMYK Print Export tab's
    ``render``: for each SVG, merges the global correction map + active
    semantic theme + per-illustration override (:func:`merge_with_semantic`),
    unions the device-CMYK overrides the same way, threads the per-file
    ``apply_auto_fix`` flag through ``dataclasses.replace``, and marks the
    illustration "exported" on a successful conversion.
    """

    def _plan(path: Path) -> BatchFilePlan:
        illu = store.load_illustration(path.name)
        merged = merge_with_semantic(
            cmyk_global, illu.cmyk_overrides, sem, "cmyk",
        )
        device_mapping = {
            **cmyk_device_global,
            **illu.cmyk_device_overrides,
        }
        per_ctx = replace(ctx, apply_auto_fix=illu.cmyk_auto_fix)

        def _mark_exported(_r: FileResult) -> None:
            illu.with_cmyk_status("exported")
            store.save_illustration(illu)

        return BatchFilePlan(merged, per_ctx, device_mapping, _mark_exported)

    return _plan
