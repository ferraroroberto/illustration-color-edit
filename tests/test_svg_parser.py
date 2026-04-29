"""Tests for src.svg_parser."""

from __future__ import annotations

import textwrap

import pytest

from src.svg_parser import (
    extract_unique_colors,
    iter_color_tokens,
    normalize_hex,
    parse_style_attribute,
    parse_svg,
)


# --------------------------------------------------------------------------- #
# normalize_hex
# --------------------------------------------------------------------------- #
class TestNormalizeHex:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("#FF0000", "#FF0000"),
            ("#ff0000", "#FF0000"),
            ("#F00", "#FF0000"),
            ("#f00", "#FF0000"),
            ("#FF0000FF", "#FF0000"),  # 8-digit hex with alpha -> drop alpha
            ("#F00A", "#FF0000"),       # 4-digit hex with alpha
            ("rgb(255, 0, 0)", "#FF0000"),
            ("rgb(255,0,0)", "#FF0000"),
            ("rgb( 255 , 0 , 0 )", "#FF0000"),
            ("rgba(255, 0, 0, 0.5)", "#FF0000"),
            ("rgb(100%, 0%, 0%)", "#FF0000"),
            ("red", "#FF0000"),
            ("Red", "#FF0000"),
            ("CORNFLOWERBLUE", "#6495ED"),
        ],
    )
    def test_valid_forms_normalize(self, raw, expected):
        assert normalize_hex(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "  ",
            "none",
            "transparent",
            "currentColor",
            "inherit",
            "url(#grad1)",
            "notacolor",
            "#GG0000",
            "#12",
            "#1234567890",
        ],
    )
    def test_invalid_or_non_color_returns_none(self, raw):
        assert normalize_hex(raw) is None


# --------------------------------------------------------------------------- #
# parse_style_attribute
# --------------------------------------------------------------------------- #
def test_parse_style_attribute_basic():
    out = parse_style_attribute("fill: #ff0000; stroke:#00FF00;opacity:.5")
    assert out == [("fill", "#ff0000"), ("stroke", "#00FF00"), ("opacity", ".5")]


def test_parse_style_attribute_empty():
    assert parse_style_attribute("") == []
    assert parse_style_attribute(";;;") == []


# --------------------------------------------------------------------------- #
# iter_color_tokens
# --------------------------------------------------------------------------- #
def test_iter_color_tokens_in_css_block():
    css = """
    .a { fill: #ff0000; }
    .b { stroke: rgb(0, 255, 0); }
    .c { fill: cornflowerblue; background: #abc; }
    """
    found = {norm for _, norm in iter_color_tokens(css)}
    assert {"#FF0000", "#00FF00", "#6495ED", "#AABBCC"}.issubset(found)


def test_iter_color_tokens_does_not_match_words_inside_rgb():
    # "rgb" is itself a 3-letter word; ensure we don't double-count it as a name.
    raw = "rgb(0, 0, 0)"
    pairs = list(iter_color_tokens(raw))
    norms = [n for _, n in pairs]
    assert norms == ["#000000"]


def test_iter_color_tokens_skips_arbitrary_words():
    raw = "fill is set to #FF0000 here"
    norms = [n for _, n in iter_color_tokens(raw)]
    # 'fill', 'is', 'set', 'to', 'here' are not named colors -> skipped
    assert norms == ["#FF0000"]


# --------------------------------------------------------------------------- #
# parse_svg integration
# --------------------------------------------------------------------------- #
INLINE_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="0" y="0" width="50" height="50" fill="#E74C3C" stroke="#333"/>
  <circle cx="60" cy="60" r="10" style="fill: rgb(46, 204, 113); stroke:#F00"/>
  <text x="10" y="90" fill="cornflowerblue">label</text>
</svg>"""


def test_parse_svg_inline_attributes_and_style():
    parsed = parse_svg(INLINE_SVG)
    assert "#E74C3C" in parsed.colors
    assert "#333333" in parsed.colors                  # 3-digit expanded
    assert "#2ECC71" in parsed.colors                  # rgb() normalized
    assert "#FF0000" in parsed.colors                  # #F00 expanded
    assert "#6495ED" in parsed.colors                  # named
    # Counts
    assert parsed.colors["#E74C3C"].count == 1
    # Contexts
    assert "@fill" in parsed.colors["#E74C3C"].contexts


STYLE_BLOCK_SVG = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg">
  <style type="text/css"><![CDATA[
    .bad  { fill: #E74C3C; }
    .good { fill: rgb(46, 204, 113); stroke: cornflowerblue; }
  ]]></style>
  <rect class="bad" x="0" y="0" width="10" height="10"/>
  <rect class="good" x="20" y="0" width="10" height="10"/>
</svg>"""


def test_parse_svg_style_block_extraction():
    parsed = parse_svg(STYLE_BLOCK_SVG)
    assert "#E74C3C" in parsed.colors
    assert "#2ECC71" in parsed.colors
    assert "#6495ED" in parsed.colors
    assert any("style-block" in u.contexts for u in parsed.colors.values())


GRADIENT_SVG = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="g">
      <stop offset="0" stop-color="#ABCDEF"/>
      <stop offset="1" stop-color="rgb(0%, 50%, 100%)"/>
    </linearGradient>
  </defs>
  <rect fill="url(#g)" stroke="none" width="10" height="10"/>
</svg>"""


def test_parse_svg_gradient_stops_extracted_url_ignored():
    parsed = parse_svg(GRADIENT_SVG)
    assert "#ABCDEF" in parsed.colors
    assert "#0080FF" in parsed.colors
    # url(#g) and 'none' should NOT produce entries.
    assert "URL(#G)" not in parsed.colors
    assert "NONE" not in parsed.colors


def test_parse_svg_malformed_raises():
    with pytest.raises(Exception):
        parse_svg("<svg><not-closed>")


def test_extract_unique_colors_returns_counts():
    counts = extract_unique_colors(INLINE_SVG)
    assert counts["#E74C3C"] == 1


def test_parse_svg_from_bytes_and_path(tmp_path):
    p = tmp_path / "x.svg"
    p.write_text(INLINE_SVG, encoding="utf-8")

    a = parse_svg(p)
    b = parse_svg(p.read_bytes())
    c = parse_svg(INLINE_SVG)

    assert set(a.colors) == set(b.colors) == set(c.colors)
