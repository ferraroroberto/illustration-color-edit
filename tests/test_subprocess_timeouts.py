"""Every Ghostscript and Inkscape call states a timeout, and an expiry ends the step with the
module's own error naming the timeout (not the generic "failed (exit ...)" of a bad exit code).

Each test mocks ``subprocess.run`` so it raises ``TimeoutExpired`` and records the ``timeout=``
it was given, without needing the real binaries installed.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from src import subprocess_limits as limits
from src.cmyk_convert import CmykConvertError, pdf_to_preview_png, rgb_pdf_to_cmyk
from src.cmyk_tac import TacComputeError, _render_cmyk_tiff
from src.render_check import RenderCheckError, _render_pdf_png, _render_svg_png
from src.svg_to_pdf import SvgToPdfError, svg_to_pdf
from src.svg_writer import write_png_from_svg
from src.trim_to_content import TrimError, _query_drawing_bbox

_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>'


class _Expiring:
    """A ``subprocess.run`` stand-in that records its ``timeout=`` and then expires."""

    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    def __call__(self, cmd, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)


def _expect_timeout(fake: _Expiring, error: type[Exception], seconds: int, run_step) -> None:
    with pytest.raises(error) as caught:
        run_step()
    assert fake.timeouts == [seconds]
    assert f"timed out after {seconds}s" in str(caught.value)
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)


def test_rgb_pdf_to_cmyk_is_bounded_by_the_conversion_timeout(tmp_path):
    input_pdf = tmp_path / "in.pdf"
    input_pdf.write_bytes(b"%PDF-fake")
    icc_profile = tmp_path / "profile.icc"
    icc_profile.write_bytes(b"icc")
    fake = _Expiring()
    with patch("src.cmyk_convert._resolve_ghostscript", return_value="gs"), \
         patch("src.cmyk_convert.subprocess.run", fake):
        _expect_timeout(
            fake, CmykConvertError, limits.GHOSTSCRIPT_CONVERT_TIMEOUT_S,
            lambda: rgb_pdf_to_cmyk(input_pdf, tmp_path / "out.pdf", icc_profile),
        )


def test_pdf_to_preview_png_is_bounded_by_the_page_timeout(tmp_path):
    pdf_path = tmp_path / "in.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    fake = _Expiring()
    with patch("src.cmyk_convert._resolve_ghostscript", return_value="gs"), \
         patch("src.cmyk_convert.subprocess.run", fake):
        _expect_timeout(
            fake, CmykConvertError, limits.GHOSTSCRIPT_PAGE_TIMEOUT_S,
            lambda: pdf_to_preview_png(pdf_path, tmp_path / "out.png"),
        )


def test_render_cmyk_tiff_is_bounded_by_the_page_timeout(tmp_path):
    fake = _Expiring()
    with patch("src.cmyk_tac._resolve_ghostscript", return_value="gs"), \
         patch("src.cmyk_tac.subprocess.run", fake):
        _expect_timeout(
            fake, TacComputeError, limits.GHOSTSCRIPT_PAGE_TIMEOUT_S,
            lambda: _render_cmyk_tiff(tmp_path / "in.pdf", tmp_path / "out.tiff", "gs", 150),
        )


def test_render_svg_png_is_bounded_by_the_inkscape_timeout(tmp_path):
    fake = _Expiring()
    with patch("src.render_check._resolve_inkscape", return_value="inkscape"), \
         patch("src.render_check.subprocess.run", fake):
        _expect_timeout(
            fake, RenderCheckError, limits.INKSCAPE_RENDER_TIMEOUT_S,
            lambda: _render_svg_png(tmp_path / "in.svg", tmp_path / "out.png", 150, "inkscape"),
        )


def test_render_pdf_png_is_bounded_by_the_page_timeout(tmp_path):
    fake = _Expiring()
    with patch("src.render_check._resolve_ghostscript", return_value="gs"), \
         patch("src.render_check.subprocess.run", fake):
        _expect_timeout(
            fake, RenderCheckError, limits.GHOSTSCRIPT_PAGE_TIMEOUT_S,
            lambda: _render_pdf_png(tmp_path / "in.pdf", tmp_path / "out.png", 150, "gs"),
        )


def test_write_png_from_svg_is_bounded_by_the_inkscape_timeout(tmp_path):
    fake = _Expiring()
    with patch("subprocess.run", fake):
        _expect_timeout(
            fake, RuntimeError, limits.INKSCAPE_RENDER_TIMEOUT_S,
            lambda: write_png_from_svg(tmp_path / "in.svg", tmp_path / "out.png", inkscape_exe="inkscape"),
        )


def test_svg_to_pdf_is_bounded_by_the_inkscape_timeout(tmp_path):
    svg_path = tmp_path / "in.svg"
    svg_path.write_text(_SVG)
    fake = _Expiring()
    with patch("src.svg_to_pdf._resolve_inkscape", return_value="inkscape"), \
         patch("src.svg_to_pdf.subprocess.run", fake):
        _expect_timeout(
            fake, SvgToPdfError, limits.INKSCAPE_RENDER_TIMEOUT_S,
            lambda: svg_to_pdf(svg_path, tmp_path / "out.pdf", width_inches=5, height_inches=5),
        )


def test_query_drawing_bbox_is_bounded_by_the_inkscape_timeout(tmp_path):
    svg_path = tmp_path / "in.svg"
    svg_path.write_text(_SVG)
    fake = _Expiring()
    with patch("src.trim_to_content._resolve_inkscape", return_value="inkscape"), \
         patch("src.trim_to_content.subprocess.run", fake):
        _expect_timeout(
            fake, TrimError, limits.INKSCAPE_RENDER_TIMEOUT_S,
            lambda: _query_drawing_bbox(svg_path, "inkscape"),
        )
