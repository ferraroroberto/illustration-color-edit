"""Tests for src.qa_report."""

from __future__ import annotations

from pathlib import Path

from src.cmyk_pipeline import BatchReport, FileResult
from src.qa_report import render_report, write_report


def _make_report() -> BatchReport:
    r = BatchReport(
        started_at="2026-05-07T10:00:00+00:00",
        finished_at="2026-05-07T10:00:05+00:00",
        total_seconds=5.12,
        icc_profile="profiles/ISOcoated_v2_eci.icc",
        pdfx=True,
        width_inches=5.5,
        height_inches=7.5,
        bleed_inches=0.125,
        palette={"#E74C3C": 3, "#000000": 5},
        palette_mapped={"#E74C3C": "#D14B3C", "#000000": "#0A0A0A"},
        files=[
            FileResult(
                filename="a.svg",
                status="ok",
                output_pdf=Path("a_CMYK.pdf"),
                preview_png=Path("a_CMYK_preview.png"),
                replacements=4,
                unmapped_colors=[],
                warnings=[],
                elapsed_seconds=2.4,
            ),
            FileResult(
                filename="b.svg",
                status="error",
                replacements=0,
                unmapped_colors=["#FE0102"],
                warnings=["embedded raster <image> elements found — be careful."],
                error="Ghostscript failed (exit 1): bad ICC",
                elapsed_seconds=0.6,
            ),
        ],
    )
    return r


def test_render_report_contains_key_facts():
    r = _make_report()
    html = render_report(r, Path("."))
    assert "<title>" in html
    assert "ISOcoated_v2_eci.icc" in html
    assert "1 succeeded" in html
    assert "1 failed" in html
    assert "5.500" in html or "5.5" in html  # width
    assert "#E74C3C" in html
    assert "#D14B3C" in html  # mapped target
    assert "Ghostscript failed" in html
    assert "&lt;image&gt;" in html  # warning text was html-escaped


def test_render_report_handles_empty_run():
    r = BatchReport(started_at="x", finished_at="y", icc_profile="p.icc")
    html = render_report(r, Path("."))
    assert "0 succeeded" in html
    assert "0 failed" in html
    assert "No colors extracted" in html
    assert "No files processed" in html


def test_write_report_writes_to_disk(tmp_path):
    r = _make_report()
    p = write_report(r, tmp_path)
    assert p.exists()
    assert p.name == "cmyk_qa_report.html"
    assert "ISOcoated" in p.read_text(encoding="utf-8")
