"""Regression tests for the Windows CREATE_NO_WINDOW convention.

Every ``subprocess.run`` call that shells out to Ghostscript or Inkscape
must pass ``creationflags`` so a console-less parent (Streamlit via
pythonw, a tray app, a scheduled task) doesn't flash a console window on
each spawn (fleet-config#399). These tests mock ``subprocess.run`` in each
module and assert the flag is present on every call site, without needing
the real binaries installed.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from src.cmyk_convert import CmykConvertError, get_ghostscript_version, pdf_to_preview_png, rgb_pdf_to_cmyk
from src.cmyk_tac import TacComputeError, _render_cmyk_tiff
from src.render_check import RenderCheckError, _render_pdf_png, _render_svg_png
from src.svg_to_pdf import SvgToPdfError, svg_to_pdf
from src.svg_writer import write_png_from_svg
from src.trim_to_content import TrimError, _query_drawing_bbox

_EXPECTED_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_FAILED_RESULT = MagicMock(returncode=1, stdout="", stderr="mocked failure")


def _assert_no_window(mock_run: MagicMock) -> None:
    assert mock_run.called, "subprocess.run was never invoked"
    for call in mock_run.call_args_list:
        assert call.kwargs.get("creationflags") == _EXPECTED_FLAGS


def test_get_ghostscript_version_sets_creationflags():
    with patch("src.cmyk_convert._resolve_ghostscript", return_value="gs"), \
         patch("src.cmyk_convert.subprocess.run", return_value=MagicMock(returncode=0, stdout="v1", stderr="")) as mock_run:
        get_ghostscript_version("gs")
    _assert_no_window(mock_run)


def test_rgb_pdf_to_cmyk_sets_creationflags(tmp_path):
    input_pdf = tmp_path / "in.pdf"
    input_pdf.write_bytes(b"%PDF-fake")
    icc_profile = tmp_path / "profile.icc"
    icc_profile.write_bytes(b"icc")
    output_pdf = tmp_path / "out.pdf"
    with patch("src.cmyk_convert._resolve_ghostscript", return_value="gs"), \
         patch("src.cmyk_convert.subprocess.run", return_value=_FAILED_RESULT) as mock_run:
        with pytest.raises(CmykConvertError):
            rgb_pdf_to_cmyk(input_pdf, output_pdf, icc_profile)
    _assert_no_window(mock_run)


def test_pdf_to_preview_png_sets_creationflags(tmp_path):
    pdf_path = tmp_path / "in.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    png_path = tmp_path / "out.png"
    with patch("src.cmyk_convert._resolve_ghostscript", return_value="gs"), \
         patch("src.cmyk_convert.subprocess.run", return_value=_FAILED_RESULT) as mock_run:
        with pytest.raises(CmykConvertError):
            pdf_to_preview_png(pdf_path, png_path)
    _assert_no_window(mock_run)


def test_render_cmyk_tiff_sets_creationflags(tmp_path):
    pdf_path = tmp_path / "in.pdf"
    tiff_path = tmp_path / "out.tiff"
    with patch("src.cmyk_tac._resolve_ghostscript", return_value="gs"), \
         patch("src.cmyk_tac.subprocess.run", return_value=_FAILED_RESULT) as mock_run:
        with pytest.raises(TacComputeError):
            _render_cmyk_tiff(pdf_path, tiff_path, "gs", 150)
    _assert_no_window(mock_run)


def test_render_svg_png_sets_creationflags(tmp_path):
    svg_path = tmp_path / "in.svg"
    png_path = tmp_path / "out.png"
    with patch("src.render_check._resolve_inkscape", return_value="inkscape"), \
         patch("src.render_check.subprocess.run", return_value=_FAILED_RESULT) as mock_run:
        with pytest.raises(RenderCheckError):
            _render_svg_png(svg_path, png_path, 150, "inkscape")
    _assert_no_window(mock_run)


def test_render_pdf_png_sets_creationflags(tmp_path):
    pdf_path = tmp_path / "in.pdf"
    png_path = tmp_path / "out.png"
    with patch("src.render_check._resolve_ghostscript", return_value="gs"), \
         patch("src.render_check.subprocess.run", return_value=_FAILED_RESULT) as mock_run:
        with pytest.raises(RenderCheckError):
            _render_pdf_png(pdf_path, png_path, 150, "gs")
    _assert_no_window(mock_run)


def test_write_png_from_svg_sets_creationflags(tmp_path):
    svg_path = tmp_path / "in.svg"
    destination = tmp_path / "out.png"
    with patch("subprocess.run", return_value=_FAILED_RESULT) as mock_run:
        with pytest.raises(RuntimeError):
            write_png_from_svg(svg_path, destination, inkscape_exe="inkscape")
    _assert_no_window(mock_run)


def test_svg_to_pdf_sets_creationflags(tmp_path):
    svg_path = tmp_path / "in.svg"
    svg_path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>')
    pdf_path = tmp_path / "out.pdf"
    with patch("src.svg_to_pdf._resolve_inkscape", return_value="inkscape"), \
         patch("src.svg_to_pdf.subprocess.run", return_value=_FAILED_RESULT) as mock_run:
        with pytest.raises(SvgToPdfError):
            svg_to_pdf(svg_path, pdf_path, width_inches=5, height_inches=5)
    _assert_no_window(mock_run)


def test_query_drawing_bbox_sets_creationflags(tmp_path):
    svg_path = tmp_path / "in.svg"
    svg_path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>')
    with patch("src.trim_to_content._resolve_inkscape", return_value="inkscape"), \
         patch("src.trim_to_content.subprocess.run", return_value=_FAILED_RESULT) as mock_run:
        with pytest.raises(TrimError):
            _query_drawing_bbox(svg_path, "inkscape")
    _assert_no_window(mock_run)
