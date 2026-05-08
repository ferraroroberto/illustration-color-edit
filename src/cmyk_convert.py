"""RGB PDF → CMYK PDF conversion via Ghostscript with an ICC profile.

This is the second stage of the CMYK print pipeline. Ghostscript reads an
RGB PDF (produced by :mod:`src.svg_to_pdf`) and emits a PDF whose color space
is DeviceCMYK, with all colors converted through the supplied ICC profile.

Optionally produces a PDF/X-1a:2003 compliant file (``pdfx=True``). Note that
PDF/X-1a forbids transparency — Inkscape-exported SVGs with semi-transparent
fills may fail compliance; in that case Ghostscript will warn and the output
may not be strictly compliant. See ``docs/2026-05-07-cmyk-pipeline.md``.

Also exposes :func:`pdf_to_preview_png` for soft-proof rendering: re-rasterise
a CMYK PDF page back to PNG via Ghostscript so the user can visually verify
the press-side result without leaving the app.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class GhostscriptNotFoundError(RuntimeError):
    """Raised when the Ghostscript binary cannot be located."""


class IccProfileNotFoundError(RuntimeError):
    """Raised when the configured ICC profile path does not exist."""


class CmykConvertError(RuntimeError):
    """Raised when Ghostscript exits non-zero during conversion."""


def _resolve_ghostscript(gs_exe: str) -> str:
    """Return a runnable Ghostscript path or raise :class:`GhostscriptNotFoundError`."""
    if Path(gs_exe).is_file():
        return gs_exe
    # Try the literal name and common Windows variants.
    for candidate in (gs_exe, "gswin64c", "gswin32c", "gs"):
        found = shutil.which(candidate)
        if found:
            return found
    raise GhostscriptNotFoundError(
        f"Ghostscript binary not found (tried {gs_exe!r}, gswin64c, gswin32c, gs). "
        "Install Ghostscript:\n"
        "  Windows: https://ghostscript.com/releases/gsdnld.html "
        "(installer adds gswin64c.exe to PATH)\n"
        "  macOS:   brew install ghostscript\n"
        "  Linux:   apt install ghostscript / dnf install ghostscript\n"
        "Then set `cmyk_export.ghostscript_path` in config.json if not on PATH."
    )


def get_ghostscript_version(gs_exe: str) -> str:
    """Return a short version string (e.g. "GPL Ghostscript 10.07.0").

    Used in audit reports so a book editor or prepress operator can see
    exactly which Ghostscript build produced a given CMYK PDF. Returns
    ``"unknown"`` if the binary cannot be located or the call fails — the
    report stays informative without becoming a hard dependency.

    Uses ``--version`` (which prints just the bare version number and
    exits) rather than ``-v``: the latter is the verbose banner and on
    some Windows builds of ``gswin64c`` waits on stdin for PostScript,
    causing this probe to time out.
    """
    try:
        bin_path = _resolve_ghostscript(gs_exe)
    except GhostscriptNotFoundError:
        return "unknown"
    try:
        result = subprocess.run(
            [bin_path, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return "unknown"
    out = (result.stdout or result.stderr or "").strip().splitlines()
    if not out:
        return "unknown"
    bare = out[0].strip()
    # ``--version`` prints just the number (e.g. "10.07.0"). Prefix it so
    # the report line reads naturally without claiming a different product.
    return f"GPL Ghostscript {bare}" if bare and bare[0].isdigit() else bare


def _output_condition_for_profile(icc_profile: Path) -> tuple[str, str]:
    """Return (OutputConditionIdentifier, OutputCondition) for a known ICC profile.

    These strings end up inside the PDF/X OutputIntent and tell prepress
    software which printing condition the file targets. We match by the
    profile filename — the common public-registry profiles have well-known
    identifiers maintained by ICC / IDEAlliance.
    """
    name = icc_profile.name.lower()
    if "swop" in name or "uswebcoated" in name:
        return ("CGATS TR 001", "U.S. Web Coated (SWOP) v2")
    if "gracol" in name:
        return ("CGATS TR 006", "Coated GRACoL 2006 (ISO 12647-2:2004)")
    if "isocoatedv2" in name or "iso_coated" in name:
        return ("FOGRA39L", "ISO Coated v2 (ECI)")
    # Generic fallback. Many printers accept "Custom" + the human-readable
    # filename; the real driver of color management is the embedded ICC.
    return ("Custom", icc_profile.stem)


def write_pdfx_def_ps(
    def_path: Path,
    icc_profile: Path,
    title: str,
) -> Path:
    """Write a PDFX_def.ps file declaring the OutputIntent and PDF/X markers.

    Ghostscript's ``-dPDFX=true`` switch alone does NOT produce a PDF/X file
    — it only enables additional checks. The actual PDF/X-1a markers
    (``/GTS_PDFXVersion``, ``/Trapped``, the ``/OutputIntents`` array with the
    embedded ICC profile) must come from a definition file passed to GS as
    a positional PostScript argument. See gs/lib/PDFX_def.ps in the GS
    distribution for the canonical template.

    The generated file declares PDF/X-1:2001 (the base spec PDF/X-1a:2003
    extends), embeds the ICC profile as an OutputIntent stream, and tags
    ``/Trapped /False`` since our pipeline does not perform trapping.
    """
    icc_ps = str(icc_profile).replace("\\", "/")
    # PostScript string literals use parens and ()-balance; escape any in
    # the title with backslashes.
    safe_title = title.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    cond_id, cond_label = _output_condition_for_profile(icc_profile)

    ps = f"""%!
% Auto-generated PDFX_def.ps for the illustration-color-edit pipeline.
% Embeds the ICC profile and declares PDF/X-1:2001 markers so Ghostscript
% emits a genuine PDF/X-1a:2003 file.

% Title in DocInfo.
[ /Title ({safe_title}) /DOCINFO pdfmark

% Forward-declare the OutputIntent dict and the embedded ICC stream.
[ /_objdef {{OutputIntent_PDFX}} /type /dict /OBJ pdfmark
[ /_objdef {{icc_PDFX}}          /type /stream /OBJ pdfmark

% Populate the OutputIntent dict.
[ {{OutputIntent_PDFX}} <<
  /Type /OutputIntent
  /S /GTS_PDFX
  /OutputCondition ({cond_label})
  /OutputConditionIdentifier ({cond_id})
  /RegistryName (http://www.color.org)
  /Info ({cond_label})
  /DestOutputProfile {{icc_PDFX}}
>> /PUT pdfmark

% Embed the ICC profile bytes as a stream object (N=4 for CMYK).
[ {{icc_PDFX}} << /N 4 >> /PUT pdfmark
[ {{icc_PDFX}} ({icc_ps}) (r) file /PUT pdfmark

% Hook the OutputIntent into the document Catalog.
[ {{Catalog}} << /OutputIntents [ {{OutputIntent_PDFX}} ] >> /PUT pdfmark

% Doc-level PDF/X markers. PDF/X-1a:2003 declares the same /GTS_PDFXVersion
% string as PDF/X-1:2001 (the base spec it extends).
[ /Trapped /False
  /GTS_PDFXVersion (PDF/X-1:2001)
  /DOCINFO pdfmark
"""
    def_path.parent.mkdir(parents=True, exist_ok=True)
    def_path.write_text(ps, encoding="utf-8")
    return def_path


def build_gs_command(
    input_pdf: Path,
    output_pdf: Path,
    icc_profile: Path,
    gs_exe: str,
    pdfx: bool = False,
    pdfx_def_ps: Optional[Path] = None,
) -> list[str]:
    """Build the Ghostscript command for RGB→CMYK conversion.

    Uses the ``-c <postscript> -f <input>`` form because in Ghostscript 10.x
    pdfwrite no longer accepts ``-sOutputICCProfile`` / ``-dOverrideICC`` /
    ``-sColorConversionStrategy`` as device parameters (they error with
    ``undefined in .putdeviceprops``). The documented mechanism is to push
    these as distiller params via PostScript.

    When ``pdfx=True`` the caller must also supply ``pdfx_def_ps`` — a
    PostScript definition file (see :func:`write_pdfx_def_ps`) that
    Ghostscript runs before pdfwrite produces output. Without it,
    ``-dPDFX=true`` runs the checker but emits a regular (non-PDF/X) file.

    Pure function — no side effects, no validation of file existence.
    """
    # PostScript string literals use forward slashes; backslashes would be
    # interpreted as escape introducers.
    icc_ps_path = str(icc_profile).replace("\\", "/")
    page_device_ps = "<</ColorConversionStrategy /CMYK /ProcessColorModel /DeviceCMYK>> setpagedevice"
    distiller_ps = f"<</OutputICCProfile ({icc_ps_path})>> setdistillerparams"

    cmd = [
        gs_exe,
        "-dNOPAUSE",
        "-dBATCH",
        "-dSAFER",
        "-sDEVICE=pdfwrite",
        f"-sOutputFile={output_pdf}",
    ]
    if pdfx:
        # GS picks PDF 1.4 automatically for PDF/X mode. Do *not* set
        # -dCompatibilityLevel=1.4 explicitly: in GS 10.x this combo causes
        # "/undefinedfilename in (.4)" — the value-parser leaks ".4" onto
        # the operand stack and PostScript later tries to run it as a file.
        cmd += ["-dPDFX=true"]
        if pdfx_def_ps is not None:
            # -dSAFER (default in GS 10.x) blocks the def file's
            # `(icc-path) (r) file` operator with /invalidfileaccess. The
            # documented escape hatch is to whitelist the specific path.
            cmd += [f"--permit-file-read={icc_profile}"]
            # The def file is a positional PostScript argument — GS executes
            # it in order, before -c prologues, so the OutputIntent and
            # /GTS_PDFXVersion markers land in the catalog.
            cmd += [str(pdfx_def_ps)]
    cmd += [
        "-c", page_device_ps,
        "-c", distiller_ps,
        "-f", str(input_pdf),
    ]
    return cmd


def rgb_pdf_to_cmyk(
    input_pdf: Path,
    output_pdf: Path,
    icc_profile: Path,
    gs_exe: str = "gswin64c",
    pdfx: bool = False,
    keep_pdfx_def_ps: bool = True,
) -> Path:
    """Convert an RGB PDF to a CMYK PDF using the given ICC profile.

    :param input_pdf: source RGB PDF (must exist).
    :param output_pdf: destination CMYK PDF (parent dir created if missing).
    :param icc_profile: path to the target CMYK ICC profile.
    :param gs_exe: Ghostscript binary path or name on PATH.
    :param pdfx: when True, emit PDF/X-1a:2003 (publisher-friendly).
    :param keep_pdfx_def_ps: when True (default) the PostScript definition
        file used to inject the OutputIntent is left next to the output PDF
        for inspection. When False it is deleted after a successful run; it
        is still required *during* conversion so the PDF/X markers reach the
        catalog.
    :returns: ``output_pdf``.
    :raises GhostscriptNotFoundError: if gs is not available.
    :raises IccProfileNotFoundError: if the ICC profile is missing.
    :raises FileNotFoundError: if ``input_pdf`` does not exist.
    :raises CmykConvertError: if Ghostscript fails.
    """
    input_pdf = Path(input_pdf)
    output_pdf = Path(output_pdf)
    icc_profile = Path(icc_profile)

    if not input_pdf.is_file():
        raise FileNotFoundError(f"Input PDF not found: {input_pdf}")
    if not icc_profile.is_file():
        raise IccProfileNotFoundError(
            f"ICC profile not found at {icc_profile}. "
            "Download a free profile from https://www.eci.org (ISO Coated v2) "
            "or https://www.color.org and place it in profiles/."
        )

    bin_path = _resolve_ghostscript(gs_exe)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    pdfx_def_ps: Optional[Path] = None
    if pdfx:
        # Drop the def file next to the output PDF so it's easy to inspect
        # if a PDF/X validator complains. One per output is fine — they're
        # tiny (<2 KB) and identifiers depend on the ICC profile name.
        pdfx_def_ps = output_pdf.with_suffix(".pdfx_def.ps")
        write_pdfx_def_ps(pdfx_def_ps, icc_profile, title=output_pdf.stem)

    cmd = build_gs_command(
        input_pdf, output_pdf, icc_profile, bin_path,
        pdfx=pdfx, pdfx_def_ps=pdfx_def_ps,
    )
    log.info("Ghostscript RGB→CMYK: %s → %s (pdfx=%s, profile=%s)",
             input_pdf.name, output_pdf.name, pdfx, icc_profile.name)
    log.debug("Ghostscript command: %s", cmd)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise CmykConvertError(
            f"Ghostscript failed (exit {result.returncode}) on {input_pdf.name}: "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )
    if not output_pdf.is_file():
        raise CmykConvertError(
            f"Ghostscript returned 0 but {output_pdf} was not produced. "
            f"stderr={result.stderr.strip()!r}"
        )
    if pdfx_def_ps is not None and not keep_pdfx_def_ps:
        # The def file was a required input to GS but the caller doesn't want
        # it kept around as a sidecar. missing_ok guards against odd cases
        # where GS itself removed it.
        pdfx_def_ps.unlink(missing_ok=True)
    return output_pdf


def pdf_to_preview_png(
    pdf_path: Path,
    png_path: Path,
    icc_profile: Optional[Path] = None,
    dpi: int = 150,
    gs_exe: str = "gswin64c",
) -> Path:
    """Render a (CMYK) PDF page to PNG for soft-proof viewing.

    If ``icc_profile`` is provided, Ghostscript uses it as the **input** profile
    so the rendered RGB PNG is a soft-proof of how the CMYK PDF will reproduce
    on press through that profile. If omitted, a straight render is produced.
    """
    pdf_path = Path(pdf_path)
    png_path = Path(png_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    bin_path = _resolve_ghostscript(gs_exe)
    png_path.parent.mkdir(parents=True, exist_ok=True)

    # -dGraphicsAlphaBits / -dTextAlphaBits enable subpixel antialiasing in
    # the rasterizer. Without them, png16m emits 1-bit-AA edges that look
    # jaggy and produce visible halos around colored shapes — which the user
    # then mistakes for a quality regression in the (vector) CMYK PDF itself.
    cmd = [
        bin_path,
        "-dNOPAUSE",
        "-dBATCH",
        "-dSAFER",
        "-sDEVICE=png16m",
        f"-r{dpi}",
        "-dGraphicsAlphaBits=4",
        "-dTextAlphaBits=4",
        "-dFirstPage=1",
        "-dLastPage=1",
        f"-sOutputFile={png_path}",
    ]
    if icc_profile and Path(icc_profile).is_file():
        # Use the CMYK profile as the source so the RGB PNG simulates press.
        cmd[-1:0] = [f"-sDefaultCMYKProfile={icc_profile}"]
    cmd.append(str(pdf_path))

    log.info("Ghostscript soft-proof PNG: %s → %s @ %d dpi", pdf_path.name, png_path.name, dpi)
    log.debug("Ghostscript command: %s", cmd)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise CmykConvertError(
            f"Ghostscript preview failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )
    return png_path
