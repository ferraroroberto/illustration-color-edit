from __future__ import annotations

from pathlib import Path

import pikepdf

from src.device_cmyk import (
    DeviceCmyk,
    normalize_device_cmyk_overrides,
    patch_pdf_device_cmyk_values_to_exact,
    patch_pdf_rgb_colors_to_device_cmyk,
    parse_device_cmyk,
)


def _write_rgb_pdf(path: Path) -> None:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(100, 100))
    page.Contents = pdf.make_stream(
        b"0.90588 0.29804 0.23529 rg\n"
        b"10 10 40 40 re f\n"
        b"0.90588 0.29804 0.23529 RG\n"
        b"10 10 m 50 50 l S\n"
    )
    pdf.save(path)


def test_parse_device_cmyk_accepts_string_list_and_dict() -> None:
    assert parse_device_cmyk("0/85/85/0") == DeviceCmyk(0, 85, 85, 0)
    assert parse_device_cmyk([1, 2, 3, 4]) == DeviceCmyk(1, 2, 3, 4)
    assert parse_device_cmyk({"c": 5, "m": 6, "y": 7, "k": 8}) == DeviceCmyk(5, 6, 7, 8)


def test_normalize_device_cmyk_overrides_canonicalizes_keys() -> None:
    overrides = normalize_device_cmyk_overrides({"#e74c3c": "0/85/85/0"})
    assert overrides == {"#E74C3C": DeviceCmyk(0, 85, 85, 0)}


def test_patch_pdf_rgb_colors_to_device_cmyk_rewrites_fill_and_stroke(tmp_path) -> None:
    pdf_path = tmp_path / "rgb.pdf"
    _write_rgb_pdf(pdf_path)

    report = patch_pdf_rgb_colors_to_device_cmyk(
        pdf_path,
        {"#E74C3C": DeviceCmyk(0, 85, 85, 0)},
    )

    assert report.requested == 1
    assert report.operators_rewritten == 2
    assert report.streams_rewritten == 1
    with pikepdf.open(pdf_path) as pdf:
        content = pdf.pages[0].Contents.read_bytes()
    assert b"0 0.85 0.85 0 k" in content
    assert b"0 0.85 0.85 0 K" in content
    assert b"rg" not in content
    assert b"RG" not in content


def test_patch_pdf_device_cmyk_values_to_exact_snaps_quantized_values(tmp_path) -> None:
    pdf_path = tmp_path / "cmyk.pdf"
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(100, 100))
    page.Contents = pdf.make_stream(
        b"0 0.849609 0.849609 0 k\n10 10 40 40 re f\n"
    )
    pdf.save(pdf_path)

    report = patch_pdf_device_cmyk_values_to_exact(
        pdf_path,
        {"#E74C3C": DeviceCmyk(0, 85, 85, 0)},
    )

    assert report.final_operators_rewritten == 1
    assert report.final_streams_rewritten == 1
    with pikepdf.open(pdf_path) as pdf:
        content = pdf.pages[0].Contents.read_bytes()
    assert b"0 0.85 0.85 0 k" in content
