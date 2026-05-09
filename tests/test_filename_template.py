"""Tests for src.filename_template."""

from __future__ import annotations

import pytest

from src.filename_template import (
    TemplateError,
    apply_template,
    parse_chapter_figure,
)


class TestParseChapterFigure:
    @pytest.mark.parametrize(
        "stem, expected",
        [
            # Two-digit, dot separator (the current convention).
            ("04.03 - venn diagram two", ("04", "03")),
            # One-digit, dot separator.
            ("1.2 description", ("1", "2")),
            ("1.2 - dashes too", ("1", "2")),
            # Dash separator.
            ("4-3 something", ("4", "3")),
            # Underscore separator.
            ("4_3 something", ("4", "3")),
            # Trailing whitespace right after.
            ("04.03", ("04", "03")),
            # Three-digit chapter, two-digit figure.
            ("100.5 cosmic", ("100", "5")),
        ],
    )
    def test_matches(self, stem, expected):
        assert parse_chapter_figure(stem) == expected

    @pytest.mark.parametrize(
        "stem",
        [
            # No leading digits.
            "books pile - learn from mistakes",
            # Has digits but no separator-figure pair.
            "v2 final",
            # Leading non-digit.
            "fig 1.2",
            # Empty.
            "",
            # Lonely chapter.
            "04 something",
        ],
    )
    def test_no_match(self, stem):
        assert parse_chapter_figure(stem) is None


class TestApplyTemplate:
    def test_empty_template_returns_stem(self):
        assert apply_template("", "anything") == "anything"
        assert apply_template("   ", "anything") == "anything"

    def test_stem_placeholder(self):
        assert apply_template("{stem}_CMYK", "foo bar") == "foo bar_CMYK"

    def test_chapter_figure_raw(self):
        # Raw form preserves the user's leading-zero convention.
        assert (
            apply_template("fig_{chapter}_{figure}", "04.03 - venn")
            == "fig_04_03"
        )
        assert (
            apply_template("fig_{chapter}_{figure}", "1.2 simple")
            == "fig_1_2"
        )

    def test_chapter_figure_padded(self):
        # Padded form forces two-digit width regardless of source.
        assert (
            apply_template("fig_{chapter:02d}_{figure:02d}_CMYK", "1.2 simple")
            == "fig_01_02_CMYK"
        )
        assert (
            apply_template("fig_{chapter:02d}_{figure:02d}_CMYK", "04.03 - venn")
            == "fig_04_03_CMYK"
        )

    def test_description_strips_prefix(self):
        assert (
            apply_template("{description}", "04.03 - venn diagram two")
            == "venn diagram two"
        )
        # No prefix → description equals the full stem.
        assert apply_template("{description}", "books pile") == "books pile"

    def test_slug(self):
        out = apply_template("{slug}", "04.03 - Venn Diagram Two")
        assert out == "venn-diagram-two"

    def test_chapter_required_but_missing_raises(self):
        with pytest.raises(TemplateError, match="chapter"):
            apply_template("{chapter}_{figure}", "books pile - no prefix")

    def test_unknown_placeholder_raises(self):
        with pytest.raises(TemplateError, match="unknown placeholder"):
            apply_template("{nope}", "04.03 - venn")

    def test_combined_template(self):
        out = apply_template(
            "{chapter:02d}-{figure:02d}_{slug}_CMYK",
            "4.3 - The Quick Brown Fox",
        )
        assert out == "04-03_the-quick-brown-fox_CMYK"
