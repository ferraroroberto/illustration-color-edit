"""Tests for src.force_k fine-line / small-text detection."""

from __future__ import annotations

import textwrap

from src.force_k import find_fine_lines


def test_no_lines_no_text():
    svg = textwrap.dedent("""\
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"
             width="100" height="100">
          <rect width="100" height="100" fill="#FF0000"/>
        </svg>
    """)
    r = find_fine_lines(svg, trim_inches=(5.5, 5.5))
    assert r.stroke_count == 0
    assert r.text_count == 0
    assert r.total == 0


def test_thin_black_stroke_detected():
    # viewBox is 100x100, trim 1in => 100 user units = 72 pt.
    # stroke-width=0.5 user units => 0.36 pt — under default 0.5 pt threshold.
    svg = textwrap.dedent("""\
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"
             width="100" height="100">
          <line x1="0" y1="0" x2="100" y2="100" stroke="#000000" stroke-width="0.5"/>
        </svg>
    """)
    r = find_fine_lines(svg, trim_inches=(1.0, 1.0))
    assert r.stroke_count == 1
    assert r.samples[0].kind == "stroke"


def test_thick_black_stroke_not_detected():
    # 2 user units * (72 / 100) = 1.44 pt — well above the threshold.
    svg = textwrap.dedent("""\
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"
             width="100" height="100">
          <line x1="0" y1="0" x2="100" y2="100" stroke="#000000" stroke-width="2"/>
        </svg>
    """)
    r = find_fine_lines(svg, trim_inches=(1.0, 1.0))
    assert r.stroke_count == 0


def test_thin_red_stroke_not_detected():
    # Color too far from black — not flagged.
    svg = textwrap.dedent("""\
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"
             width="100" height="100">
          <line x1="0" y1="0" x2="100" y2="100" stroke="#FF0000" stroke-width="0.5"/>
        </svg>
    """)
    r = find_fine_lines(svg, trim_inches=(1.0, 1.0))
    assert r.stroke_count == 0


def test_near_black_stroke_detected():
    # #1A1A1A has ΔE76 ~ 6 from pure black — within the 8 cutoff.
    svg = textwrap.dedent("""\
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"
             width="100" height="100">
          <line x1="0" y1="0" x2="100" y2="100" stroke="#1A1A1A" stroke-width="0.4"/>
        </svg>
    """)
    r = find_fine_lines(svg, trim_inches=(1.0, 1.0))
    assert r.stroke_count == 1


def test_small_black_text_detected():
    # font-size=10 user units * (72/100) = 7.2 pt — under default 9 pt.
    svg = textwrap.dedent("""\
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"
             width="100" height="100">
          <text x="10" y="50" font-size="10" fill="#000000">tiny annotation</text>
        </svg>
    """)
    r = find_fine_lines(svg, trim_inches=(1.0, 1.0))
    assert r.text_count == 1
    assert r.samples[0].kind == "text"
    assert "tiny annotation" in r.samples[0].sample


def test_large_text_not_detected():
    # font-size=20 user units * (72/100) = 14.4 pt — above default 9 pt threshold.
    svg = textwrap.dedent("""\
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"
             width="100" height="100">
          <text x="10" y="50" font-size="20" fill="#000000">heading</text>
        </svg>
    """)
    r = find_fine_lines(svg, trim_inches=(1.0, 1.0))
    assert r.text_count == 0


def test_summary_counts():
    svg = textwrap.dedent("""\
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"
             width="100" height="100">
          <line x1="0" y1="0" x2="100" y2="100" stroke="#000000" stroke-width="0.5"/>
          <text x="10" y="50" font-size="10" fill="#000000">x</text>
        </svg>
    """)
    r = find_fine_lines(svg, trim_inches=(1.0, 1.0))
    assert r.total == 2
    assert "fine stroke" in r.summary()
    assert "small text" in r.summary()


def test_explicit_pt_units():
    # stroke-width="0.4pt" — 0.4 pt regardless of viewBox/trim.
    svg = textwrap.dedent("""\
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000"
             width="1000" height="1000">
          <line x1="0" y1="0" x2="100" y2="100" stroke="#000000" stroke-width="0.4pt"/>
        </svg>
    """)
    r = find_fine_lines(svg, trim_inches=(5.5, 5.5))
    assert r.stroke_count == 1
    assert abs(r.samples[0].size_pt - 0.4) < 0.01


def test_malformed_returns_empty_report():
    r = find_fine_lines("<not valid svg", trim_inches=(1.0, 1.0))
    assert r.total == 0
