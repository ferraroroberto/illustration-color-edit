"""Smoke tests for src.print_safety."""

from __future__ import annotations

from src.config import PrintSafetyConfig
from src.print_safety import check_mapping, check_target


def test_check_target_safe_returns_none():
    cfg = PrintSafetyConfig(min_gray_value="#EEEEEE", warn_only=True)
    assert check_target("#333333", cfg) is None


def test_check_target_too_light_warns():
    cfg = PrintSafetyConfig(min_gray_value="#EEEEEE")
    w = check_target("#F5F5F5", cfg)
    assert w is not None
    assert "lighter" in w.reason


def test_check_target_non_gray_warns():
    cfg = PrintSafetyConfig(min_gray_value="#EEEEEE")
    w = check_target("#444433", cfg, sources=["#FF0000"])
    assert w is not None
    assert "not grayscale" in w.reason
    assert w.sources == ("#FF0000",)


def test_check_mapping_groups_sources_by_target():
    cfg = PrintSafetyConfig(min_gray_value="#EEEEEE")
    mapping = {"#FF0000": "#FAFAFA", "#00FF00": "#FAFAFA", "#0000FF": "#222222"}
    warnings = check_mapping(mapping, cfg)
    # Only #FAFAFA should warn.
    assert len(warnings) == 1
    w = warnings[0]
    assert w.target == "#FAFAFA"
    assert set(w.sources) == {"#FF0000", "#00FF00"}
