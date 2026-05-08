"""Tests for the CMYK gamut helper.

Pure helpers (hex parsing, sRGB→Lab, ΔE76) are tested with concrete values.
The full ICC roundtrip is exercised against the project's own
``profiles/USWebCoatedSWOP.icc`` if present — skipped otherwise so the
suite stays runnable on machines without the profile.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.cmyk_gamut import (
    _hex_to_rgb,
    _srgb_to_lab,
    cmyk_gamut_delta,
    delta_e_76,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SWOP_PROFILE = PROJECT_ROOT / "profiles" / "USWebCoatedSWOP.icc"


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_hex_to_rgb_strips_hash_and_uppercases():
    assert _hex_to_rgb("#FF8000") == (255, 128, 0)
    assert _hex_to_rgb("ff8000") == (255, 128, 0)


def test_srgb_to_lab_white_is_about_100_0_0():
    L, a, b = _srgb_to_lab(255, 255, 255)
    assert L == pytest.approx(100, abs=0.5)
    assert abs(a) < 0.5
    assert abs(b) < 0.5


def test_srgb_to_lab_black_is_about_0_0_0():
    L, a, b = _srgb_to_lab(0, 0, 0)
    assert L == pytest.approx(0, abs=0.1)
    assert a == pytest.approx(0, abs=0.1)
    assert b == pytest.approx(0, abs=0.1)


def test_delta_e_76_identical_is_zero():
    assert delta_e_76((100, 100, 100), (100, 100, 100)) == 0.0


def test_delta_e_76_white_to_black_is_large():
    # Lab L spans 0-100, so ΔE76 between black and white is ~100.
    assert delta_e_76((0, 0, 0), (255, 255, 255)) == pytest.approx(100, abs=1)


# --------------------------------------------------------------------------- #
# ICC roundtrip — only runs if the SWOP profile is present
# --------------------------------------------------------------------------- #
needs_swop = pytest.mark.skipif(
    not SWOP_PROFILE.is_file(),
    reason=f"SWOP profile not present at {SWOP_PROFILE}",
)


@needs_swop
def test_gamut_delta_white_is_near_zero():
    """White round-trips cleanly — minimum-ink CMYK ≈ paper white."""
    d = cmyk_gamut_delta("#FFFFFF", SWOP_PROFILE)
    assert d is not None
    assert d < 2.0, f"expected near-zero, got ΔE={d:.2f}"


@needs_swop
def test_gamut_delta_black_is_low():
    """Pure black is well within CMYK gamut (K=100)."""
    d = cmyk_gamut_delta("#000000", SWOP_PROFILE)
    assert d is not None
    assert d < 5.0, f"expected low, got ΔE={d:.2f}"


@needs_swop
def test_gamut_delta_saturated_red_is_high():
    """sRGB pure red sits outside SWOP gamut — should flag clearly."""
    d = cmyk_gamut_delta("#FF0000", SWOP_PROFILE)
    assert d is not None
    assert d > 6.0, f"expected high (>6), got ΔE={d:.2f}"


@needs_swop
def test_gamut_delta_returns_none_for_missing_profile(tmp_path):
    bogus = tmp_path / "does_not_exist.icc"
    assert cmyk_gamut_delta("#FF0000", bogus) is None


@needs_swop
def test_gamut_delta_caching_returns_consistent_results():
    """The lru_cached transform should not produce drift across calls."""
    d1 = cmyk_gamut_delta("#46AA3A", SWOP_PROFILE)
    d2 = cmyk_gamut_delta("#46AA3A", SWOP_PROFILE)
    assert d1 == d2
