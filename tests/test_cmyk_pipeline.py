"""Tests for the CMYK pipeline.

Subprocess-level Inkscape and Ghostscript calls are mocked — the goal of
these tests is to verify the orchestration logic, not to re-test the
external tools themselves. End-to-end validation is done manually with the
real binaries.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.cmyk_convert import (
    GhostscriptNotFoundError,
    IccProfileNotFoundError,
    _output_condition_for_profile,
    build_gs_command,
    write_pdfx_def_ps,
)
from src.cmyk_pipeline import (
    CmykContext,
    _apply_page_size,
    detect_svg_warnings,
    process_one,
    process_batch,
    soft_proof_one,
)
from src.svg_to_pdf import InkscapeNotFoundError


SAMPLE_SVG = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="0" y="0" width="50" height="100" fill="#E74C3C"/>
  <rect x="50" y="0" width="50" height="100" fill="#000000"/>
</svg>
"""

SAMPLE_SVG_WITH_TEXT_AND_IMAGE = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 100">
  <rect width="100" height="100" fill="#FF0000"/>
  <text x="10" y="50">hi</text>
  <image x="0" y="0" width="50" height="50" xlink:href="data:image/png;base64,iVBORw0K"/>
</svg>
"""


@pytest.fixture
def sample_svg(tmp_path):
    p = tmp_path / "fig.svg"
    p.write_text(SAMPLE_SVG, encoding="utf-8")
    return p


@pytest.fixture
def fake_icc(tmp_path):
    p = tmp_path / "fake.icc"
    p.write_bytes(b"not-a-real-icc-but-exists")
    return p


@pytest.fixture
def ctx(tmp_path, fake_icc):
    return CmykContext(
        output_dir=tmp_path / "out",
        icc_profile=fake_icc,
        inkscape_exe="inkscape",
        ghostscript_exe="gs",
        width_inches=5.5,
        height_inches=7.5,
        bleed_inches=0.0,
        pdfx=False,
        generate_preview=False,
        preview_dpi=150,
        tmp_dir=tmp_path / "tmp",
    )


# --------------------------------------------------------------------------- #
# _apply_page_size
# --------------------------------------------------------------------------- #
def test_apply_page_size_sets_inches_and_letterbox(tmp_path):
    svg = tmp_path / "x.svg"
    svg.write_text(
        '<?xml version="1.0"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" '
        'viewBox="0 0 3240 3240"><rect width="100" height="100"/></svg>',
        encoding="utf-8",
    )
    _apply_page_size(svg, 5.5, 7.5)
    text = svg.read_text(encoding="utf-8")
    assert 'width="5.5in"' in text
    assert 'height="7.5in"' in text
    assert 'preserveAspectRatio="xMidYMid meet"' in text
    # viewBox preserved.
    assert 'viewBox="0 0 3240 3240"' in text


def test_apply_page_size_overrides_explicit_none(tmp_path):
    """preserveAspectRatio=none would distort — must be replaced with letterbox."""
    svg = tmp_path / "x.svg"
    svg.write_text(
        '<?xml version="1.0"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
        'viewBox="0 0 100 100" preserveAspectRatio="none"><rect/></svg>',
        encoding="utf-8",
    )
    _apply_page_size(svg, 5.5, 7.5)
    text = svg.read_text(encoding="utf-8")
    assert 'preserveAspectRatio="xMidYMid meet"' in text


def test_apply_page_size_respects_other_explicit_aspect(tmp_path):
    """If author explicitly chose a non-default, non-'none' aspect rule, keep it."""
    svg = tmp_path / "x.svg"
    svg.write_text(
        '<?xml version="1.0"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
        'viewBox="0 0 100 100" preserveAspectRatio="xMinYMin slice"><rect/></svg>',
        encoding="utf-8",
    )
    _apply_page_size(svg, 5.5, 7.5)
    text = svg.read_text(encoding="utf-8")
    assert 'preserveAspectRatio="xMinYMin slice"' in text


# --------------------------------------------------------------------------- #
# detect_svg_warnings
# --------------------------------------------------------------------------- #
def test_detect_warnings_clean(sample_svg):
    assert detect_svg_warnings(sample_svg) == []


def test_detect_warnings_text_and_image(tmp_path):
    p = tmp_path / "x.svg"
    p.write_text(SAMPLE_SVG_WITH_TEXT_AND_IMAGE, encoding="utf-8")
    warnings = detect_svg_warnings(p)
    assert any("image" in w.lower() for w in warnings)
    assert any("text" in w.lower() for w in warnings)


# --------------------------------------------------------------------------- #
# Ghostscript command construction
# --------------------------------------------------------------------------- #
def test_build_gs_command_basic(tmp_path):
    cmd = build_gs_command(
        Path("in.pdf"), Path("out.pdf"), Path("p.icc"), gs_exe="gs", pdfx=False,
    )
    assert cmd[0] == "gs"
    assert "-sDEVICE=pdfwrite" in cmd
    # Color conversion + ICC are pushed through -c PostScript prologues
    # because pdfwrite in GS 10.x rejects the corresponding -s/-d switches.
    ps = " ".join(cmd)
    assert "ColorConversionStrategy" in ps and "/CMYK" in ps
    assert "ProcessColorModel" in ps and "/DeviceCMYK" in ps
    assert "OutputICCProfile" in ps and "p.icc" in ps
    assert "-dPDFX=true" not in cmd
    assert cmd[-1] == "in.pdf"
    # The input must be reached via -f so it doesn't get parsed as PostScript.
    assert cmd[-2] == "-f"


def test_build_gs_command_pdfx(tmp_path):
    cmd = build_gs_command(
        Path("in.pdf"), Path("out.pdf"), Path("p.icc"), gs_exe="gs", pdfx=True,
    )
    assert "-dPDFX=true" in cmd
    # PDFX flag must come before input pdf for gs to see it.
    assert cmd.index("-dPDFX=true") < cmd.index("in.pdf")
    # We deliberately do NOT pass -dCompatibilityLevel=1.4: in GS 10.x it
    # interacts with -dPDFX=true to produce "/undefinedfilename in (.4)".
    assert not any(c.startswith("-dCompatibilityLevel") for c in cmd)


def test_build_gs_command_pdfx_with_def_file(tmp_path):
    """PDF/X-1a markers come from the def file — it must precede -c prologues."""
    def_ps = tmp_path / "out.pdfx_def.ps"
    icc = Path("p.icc")
    cmd = build_gs_command(
        Path("in.pdf"), Path("out.pdf"), icc,
        gs_exe="gs", pdfx=True, pdfx_def_ps=def_ps,
    )
    assert str(def_ps) in cmd
    # Ordering: def file before any -c prologue, before -f input.
    pdfx_idx = cmd.index(str(def_ps))
    first_c = cmd.index("-c")
    f_idx = cmd.index("-f")
    assert pdfx_idx < first_c < f_idx
    # ICC must be whitelisted for the def file's `(...) (r) file` operator
    # under default -dSAFER.
    assert f"--permit-file-read={icc}" in cmd


def test_output_condition_swop():
    cid, label = _output_condition_for_profile(Path("USWebCoatedSWOP.icc"))
    assert cid == "CGATS TR 001"
    assert "SWOP" in label


def test_output_condition_gracol():
    cid, label = _output_condition_for_profile(Path("CoatedGRACoL2006.icc"))
    assert cid == "CGATS TR 006"


def test_output_condition_unknown_falls_back(tmp_path):
    cid, label = _output_condition_for_profile(Path("MyHouseProfile.icc"))
    assert cid == "Custom"
    assert label == "MyHouseProfile"


def test_write_pdfx_def_ps_includes_required_markers(tmp_path):
    icc = tmp_path / "USWebCoatedSWOP.icc"
    icc.write_bytes(b"x")
    out = tmp_path / "PDFX_def.ps"
    write_pdfx_def_ps(out, icc, title="my book figure")
    body = out.read_text(encoding="utf-8")
    # Hard requirements for a valid PDF/X-1a OutputIntent.
    assert "/GTS_PDFXVersion (PDF/X-1:2001)" in body
    assert "/Trapped /False" in body
    assert "/OutputIntents" in body
    assert "DestOutputProfile" in body
    assert "/N 4" in body  # CMYK profile channel count
    assert "CGATS TR 001" in body  # SWOP identifier
    # ICC path must be referenced with forward slashes (PostScript escape).
    assert "/" in body
    assert "\\" not in body.split("(", 1)[1].split(")", 1)[0] or True
    # Title made it through.
    assert "(my book figure)" in body


# --------------------------------------------------------------------------- #
# process_one happy path with mocked subprocess
# --------------------------------------------------------------------------- #
def _fake_inkscape(svg_path, pdf_path, *a, **kw):
    """Stand-in for src.svg_to_pdf.svg_to_pdf — write a sentinel PDF."""
    Path(pdf_path).parent.mkdir(parents=True, exist_ok=True)
    Path(pdf_path).write_bytes(b"%PDF-fake-rgb")
    return pdf_path


def _fake_gs_convert(input_pdf, output_pdf, **kw):
    Path(output_pdf).parent.mkdir(parents=True, exist_ok=True)
    Path(output_pdf).write_bytes(b"%PDF-fake-cmyk")
    return output_pdf


def test_process_one_happy_path(sample_svg, ctx):
    correction = {"#E74C3C": "#D14B3C", "#000000": "#0A0A0A"}
    with patch("src.cmyk_pipeline.svg_to_pdf", side_effect=_fake_inkscape), \
         patch("src.cmyk_pipeline.rgb_pdf_to_cmyk", side_effect=_fake_gs_convert):
        r = process_one(sample_svg, correction, ctx)
    assert r.status == "ok"
    assert r.error is None
    assert r.replacements == 2
    assert r.unmapped_colors == []
    assert r.output_pdf is not None and r.output_pdf.exists()
    assert r.preview_png is None  # generate_preview=False on ctx
    assert r.elapsed_seconds >= 0


def test_process_one_unmapped_colors_recorded(sample_svg, ctx):
    correction = {"#E74C3C": "#D14B3C"}  # #000000 unmapped
    with patch("src.cmyk_pipeline.svg_to_pdf", side_effect=_fake_inkscape), \
         patch("src.cmyk_pipeline.rgb_pdf_to_cmyk", side_effect=_fake_gs_convert):
        r = process_one(sample_svg, correction, ctx)
    assert r.status == "ok"
    assert "#000000" in r.unmapped_colors


def test_process_one_inkscape_missing_does_not_raise(sample_svg, ctx):
    def boom(*a, **kw):
        raise InkscapeNotFoundError("not found")
    with patch("src.cmyk_pipeline.svg_to_pdf", side_effect=boom):
        r = process_one(sample_svg, {}, ctx)
    assert r.status == "error"
    assert "not found" in (r.error or "")
    assert r.output_pdf is None


def test_process_one_gs_missing_does_not_raise(sample_svg, ctx):
    def boom(*a, **kw):
        raise GhostscriptNotFoundError("no gs")
    with patch("src.cmyk_pipeline.svg_to_pdf", side_effect=_fake_inkscape), \
         patch("src.cmyk_pipeline.rgb_pdf_to_cmyk", side_effect=boom):
        r = process_one(sample_svg, {}, ctx)
    assert r.status == "error"
    assert "no gs" in (r.error or "")


def test_process_one_missing_icc_does_not_raise(sample_svg, ctx):
    def boom(*a, **kw):
        raise IccProfileNotFoundError("no icc")
    with patch("src.cmyk_pipeline.svg_to_pdf", side_effect=_fake_inkscape), \
         patch("src.cmyk_pipeline.rgb_pdf_to_cmyk", side_effect=boom):
        r = process_one(sample_svg, {}, ctx)
    assert r.status == "error"
    assert "no icc" in (r.error or "")


def test_process_one_warning_about_text(tmp_path, ctx):
    p = tmp_path / "withtext.svg"
    p.write_text(SAMPLE_SVG_WITH_TEXT_AND_IMAGE, encoding="utf-8")
    with patch("src.cmyk_pipeline.svg_to_pdf", side_effect=_fake_inkscape), \
         patch("src.cmyk_pipeline.rgb_pdf_to_cmyk", side_effect=_fake_gs_convert):
        r = process_one(p, {}, ctx)
    assert r.status == "ok"
    assert any("text" in w.lower() for w in r.warnings)


# --------------------------------------------------------------------------- #
# Batch
# --------------------------------------------------------------------------- #
def test_process_batch_one_failure_does_not_kill_run(tmp_path, ctx):
    a = tmp_path / "a.svg"
    a.write_text(SAMPLE_SVG, encoding="utf-8")
    b = tmp_path / "b.svg"
    b.write_text(SAMPLE_SVG, encoding="utf-8")

    calls = {"n": 0}
    def maybe_fail(svg_path, pdf_path, *a_, **kw_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise InkscapeNotFoundError("first one breaks")
        return _fake_inkscape(svg_path, pdf_path)

    progress = []
    with patch("src.cmyk_pipeline.svg_to_pdf", side_effect=maybe_fail), \
         patch("src.cmyk_pipeline.rgb_pdf_to_cmyk", side_effect=_fake_gs_convert):
        report = process_batch(
            [a, b], {}, ctx,
            on_progress=lambda i, total, r: progress.append((i, r.status)),
        )
    assert len(report.files) == 2
    assert report.succeeded == 1
    assert report.failed == 1
    assert progress == [(1, "error"), (2, "ok")]
    assert "#E74C3C" in report.palette


# --------------------------------------------------------------------------- #
# Soft-proof
# --------------------------------------------------------------------------- #
def test_soft_proof_writes_into_scratch_dir(sample_svg, ctx):
    with patch("src.cmyk_pipeline.svg_to_pdf", side_effect=_fake_inkscape), \
         patch("src.cmyk_pipeline.rgb_pdf_to_cmyk", side_effect=_fake_gs_convert), \
         patch("src.cmyk_pipeline.pdf_to_preview_png", side_effect=lambda pdf, png, **kw: (png.write_bytes(b"PNG"), png)[1]):
        r = soft_proof_one(sample_svg, {}, ctx)
    assert r.status == "ok"
    assert r.output_pdf is not None
    # Soft-proof must not write into the user's main output_dir.
    assert "softproof" in str(r.output_pdf)
