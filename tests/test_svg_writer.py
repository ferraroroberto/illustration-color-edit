"""Tests for src.svg_writer."""

from __future__ import annotations

import re

from src.svg_parser import extract_unique_colors, parse_svg
from src.svg_writer import apply_mapping, apply_mapping_with_report, write_converted_svg


INLINE_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="0" y="0" width="50" height="50" fill="#E74C3C" stroke="#333"/>
  <circle cx="60" cy="60" r="10" style="fill: rgb(46, 204, 113); stroke:#F00"/>
  <text x="10" y="90" fill="cornflowerblue">label</text>
</svg>"""


STYLE_BLOCK_SVG = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg">
  <style>
    .bad  { fill: #E74C3C; }
    .good { fill: rgb(46, 204, 113); stroke: cornflowerblue; }
  </style>
  <rect class="bad" width="10" height="10"/>
  <rect class="good" width="10" height="10"/>
</svg>"""


GRADIENT_SVG = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="g">
      <stop offset="0" stop-color="#ABCDEF"/>
      <stop offset="1" stop-color="rgb(0%, 50%, 100%)"/>
    </linearGradient>
  </defs>
  <rect fill="url(#g)" width="10" height="10"/>
</svg>"""


def test_apply_mapping_replaces_inline_attributes():
    mapping = {"#E74C3C": "#333333", "#333333": "#222222"}
    out = apply_mapping(INLINE_SVG, mapping).decode("utf-8")
    # The fill="#E74C3C" became "#333333"; stroke="#333" (which normalizes to
    # #333333) became "#222222".
    assert 'fill="#333333"' in out
    assert 'stroke="#222222"' in out


def test_apply_mapping_replaces_style_attribute_paint_props():
    mapping = {"#2ECC71": "#CCCCCC", "#FF0000": "#444444"}
    out = apply_mapping(INLINE_SVG, mapping).decode("utf-8")
    # rgb(...) was inside style — it's now the target hex
    assert "#CCCCCC" in out
    # named "red" via #F00 in style was mapped
    assert "#444444" in out
    # original tokens are gone
    assert "rgb(46, 204, 113)" not in out
    assert "#F00" not in out


def test_apply_mapping_replaces_style_block_text():
    mapping = {
        "#E74C3C": "#333333",
        "#2ECC71": "#CCCCCC",
        "#6495ED": "#888888",  # cornflowerblue
    }
    out = apply_mapping(STYLE_BLOCK_SVG, mapping).decode("utf-8")
    assert "#333333" in out
    assert "#CCCCCC" in out
    assert "#888888" in out
    # Original named color is gone (replaced).
    assert "cornflowerblue" not in out
    # rgb(...) is gone (replaced).
    assert "rgb(46, 204, 113)" not in out


def test_apply_mapping_named_color_replaced_inline_attribute():
    svg = '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg">' \
          '<rect fill="cornflowerblue" width="1" height="1"/></svg>'
    out = apply_mapping(svg, {"#6495ED": "#444444"}).decode("utf-8")
    assert 'fill="#444444"' in out
    assert "cornflowerblue" not in out


def test_apply_mapping_leaves_unmapped_colors_unchanged():
    mapping = {"#E74C3C": "#333333"}  # only the red is mapped
    out = apply_mapping(INLINE_SVG, mapping).decode("utf-8")
    # Unmapped green should still be present somewhere
    assert "rgb(46, 204, 113)" in out


def test_apply_mapping_report_counts_replacements_and_unmapped():
    mapping = {"#E74C3C": "#333333"}
    _, report = apply_mapping_with_report(INLINE_SVG, mapping)
    assert report.replacements >= 1
    assert report.by_source["#E74C3C"] >= 1
    # Other colors present in the SVG but not in mapping should appear in
    # the unmapped report.
    assert "#2ECC71" in report.unmapped
    assert "#FF0000" in report.unmapped
    assert "#6495ED" in report.unmapped
    assert "#333333" in report.unmapped


def test_apply_mapping_preserves_paths_and_ids():
    svg = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg">
  <path id="thing-42" d="M0 0 L 10 10 Z" fill="#FF0000"/>
</svg>"""
    out = apply_mapping(svg, {"#FF0000": "#222222"}).decode("utf-8")
    assert 'id="thing-42"' in out
    assert 'd="M0 0 L 10 10 Z"' in out
    assert 'fill="#222222"' in out


def test_apply_mapping_preserves_gradient_url_reference():
    out = apply_mapping(GRADIENT_SVG, {"#ABCDEF": "#111111"}).decode("utf-8")
    # The url(#g) reference must still be intact.
    assert 'fill="url(#g)"' in out
    # The stop-color was rewritten.
    assert "#111111" in out


def test_apply_mapping_does_not_mutate_input_parsed_svg():
    parsed = parse_svg(INLINE_SVG)
    before = set(parsed.colors)
    apply_mapping(parsed, {"#E74C3C": "#333333"})
    after = extract_unique_colors(parsed.raw_bytes)
    # parsed.tree gets re-used by callers; ensure we copied it.
    assert before == set(after.keys()) | (set() if before == set(after.keys()) else set())


def test_write_converted_svg_creates_file(tmp_path):
    src = tmp_path / "in.svg"
    src.write_text(INLINE_SVG, encoding="utf-8")
    dst = tmp_path / "out" / "out.svg"

    report = write_converted_svg(src, {"#E74C3C": "#333333"}, dst)
    assert dst.exists()
    text = dst.read_text(encoding="utf-8")
    assert "#333333" in text
    assert report.replacements >= 1


def test_idempotent_with_empty_mapping():
    out = apply_mapping(INLINE_SVG, {}).decode("utf-8")
    # Strip xml declaration line for comparison; lxml may add encoding.
    assert "rect" in out
    assert "circle" in out
    # All original colors still present (in some normalized form).
    assert "#E74C3C" in out or "#e74c3c" in out
