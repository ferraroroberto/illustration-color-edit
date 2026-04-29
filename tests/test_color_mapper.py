"""Tests for src.color_mapper."""

from __future__ import annotations

import math

import pytest

from src.color_mapper import (
    ColorMapper,
    MatchKind,
    color_distance,
    gray_value,
    hex_to_lab,
    hex_to_rgb,
    is_grayscale,
    rgb_to_hex,
    suggest_from_history,
)
from src.config import MatchingConfig


# --------------------------------------------------------------------------- #
# Color-space helpers
# --------------------------------------------------------------------------- #
def test_hex_to_rgb_roundtrip():
    assert hex_to_rgb("#FF0000") == (255, 0, 0)
    assert rgb_to_hex((255, 0, 0)) == "#FF0000"
    assert rgb_to_hex(hex_to_rgb("#ABCDEF")) == "#ABCDEF"


def test_hex_to_lab_known_values():
    # Pure black -> L=0, a=0, b=0
    L, a, b = hex_to_lab("#000000")
    assert L == pytest.approx(0.0, abs=1e-3)
    assert a == pytest.approx(0.0, abs=1e-3)
    assert b == pytest.approx(0.0, abs=1e-3)

    # Pure white -> L=100
    L, _, _ = hex_to_lab("#FFFFFF")
    assert L == pytest.approx(100.0, abs=1e-3)


def test_color_distance_lab_zero_for_identity():
    assert color_distance("#FF0000", "#FF0000", "lab") == pytest.approx(0.0, abs=1e-9)


def test_color_distance_close_reds():
    # Two visually-close reds should be much nearer than red vs. green.
    d_close = color_distance("#E74C3C", "#E63E3E", "lab")
    d_far = color_distance("#E74C3C", "#2ECC71", "lab")
    assert d_close < 10.0
    assert d_far > 50.0


def test_color_distance_rgb_metric():
    d = color_distance("#000000", "#FFFFFF", "rgb")
    assert d == pytest.approx(math.sqrt(3 * 255**2))


def test_color_distance_unknown_metric_raises():
    with pytest.raises(ValueError):
        color_distance("#000000", "#FFFFFF", "weird")


def test_is_grayscale():
    assert is_grayscale("#808080")
    assert is_grayscale("#7F8081")  # within tolerance
    assert not is_grayscale("#FF0000")


def test_gray_value():
    assert gray_value("#000000") == 0
    assert gray_value("#FFFFFF") == 255
    # Pure red has lower luminance than pure green per Rec.709.
    assert gray_value("#FF0000") < gray_value("#00FF00")


# --------------------------------------------------------------------------- #
# ColorMapper: suggest()
# --------------------------------------------------------------------------- #
GLOBAL_MAP = {
    "#E74C3C": {"target": "#333333", "label": "red / bad", "notes": ""},
    "#2ECC71": {"target": "#CCCCCC", "label": "green / good", "notes": ""},
}


def test_suggest_exact_match():
    m = ColorMapper(global_map=GLOBAL_MAP, matching=MatchingConfig())
    s = m.suggest("#E74C3C")
    assert s.kind is MatchKind.EXACT
    assert s.target == "#333333"
    assert s.distance == 0.0


def test_suggest_exact_is_case_insensitive():
    m = ColorMapper(global_map=GLOBAL_MAP, matching=MatchingConfig())
    assert m.suggest("#e74c3c").target == "#333333"


def test_suggest_near_match_within_threshold():
    m = ColorMapper(
        global_map=GLOBAL_MAP,
        matching=MatchingConfig(nearest_enabled=True, metric="lab", threshold=15.0),
    )
    s = m.suggest("#E63E3E")  # close to #E74C3C
    assert s.kind is MatchKind.NEAR
    assert s.target == "#333333"
    assert s.via == "#E74C3C"
    assert 0 < s.distance < 15.0


def test_suggest_near_disabled_falls_through_to_none():
    m = ColorMapper(
        global_map=GLOBAL_MAP,
        matching=MatchingConfig(nearest_enabled=False, metric="lab", threshold=999.0),
    )
    s = m.suggest("#E63E3E")
    assert s.kind is MatchKind.NONE
    assert s.target is None


def test_suggest_no_match_when_threshold_too_tight():
    m = ColorMapper(
        global_map=GLOBAL_MAP,
        matching=MatchingConfig(nearest_enabled=True, metric="lab", threshold=0.1),
    )
    s = m.suggest("#0000FF")
    assert s.kind is MatchKind.NONE


def test_suggest_picks_closest_when_multiple_in_range():
    big_map = {
        "#FF0000": {"target": "#111111", "label": "red"},
        "#00FF00": {"target": "#222222", "label": "green"},
        "#0000FF": {"target": "#333333", "label": "blue"},
    }
    m = ColorMapper(
        global_map=big_map,
        matching=MatchingConfig(nearest_enabled=True, metric="lab", threshold=999.0),
    )
    s = m.suggest("#0000F0")  # closest to blue
    assert s.via == "#0000FF"
    assert s.target == "#333333"


def test_overrides_take_priority_over_global():
    m = ColorMapper(global_map=GLOBAL_MAP, matching=MatchingConfig())
    over = m.with_overrides({"#E74C3C": "#999999"})
    s = over.suggest("#E74C3C")
    assert s.kind is MatchKind.EXACT
    assert s.target == "#999999"

    # Original mapper untouched.
    assert m.suggest("#E74C3C").target == "#333333"


# --------------------------------------------------------------------------- #
# Resolve / apply_to_palette
# --------------------------------------------------------------------------- #
def test_resolve_with_manual_override():
    m = ColorMapper(global_map=GLOBAL_MAP)
    assert m.resolve("#E74C3C", manual="#abcdef") == "#ABCDEF"


def test_resolve_invalid_manual_raises():
    m = ColorMapper(global_map=GLOBAL_MAP)
    with pytest.raises(ValueError):
        m.resolve("#E74C3C", manual="not-a-color")


def test_apply_to_palette_returns_full_dict_with_unmapped_as_none():
    m = ColorMapper(
        global_map=GLOBAL_MAP,
        matching=MatchingConfig(nearest_enabled=False),
    )
    result = m.apply_to_palette(["#E74C3C", "#123456"])
    assert result["#E74C3C"] == "#333333"
    assert result["#123456"] is None


def test_apply_to_palette_honors_manual_overrides():
    m = ColorMapper(global_map=GLOBAL_MAP)
    result = m.apply_to_palette(["#E74C3C"], manual={"#E74C3C": "#000000"})
    assert result["#E74C3C"] == "#000000"


# --------------------------------------------------------------------------- #
# History suggestions
# --------------------------------------------------------------------------- #
def test_suggest_from_history_sorts_by_count_desc():
    history = {"#E74C3C": {"#222222": 1, "#333333": 4, "#111111": 2}}
    out = suggest_from_history("#e74c3c", history)
    assert out == [("#333333", 4), ("#111111", 2), ("#222222", 1)]


def test_suggest_from_history_empty_returns_empty():
    assert suggest_from_history("#FF0000", {}) == []
