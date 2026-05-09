"""Tests for src.bleed_overlay — visual guide compositing."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from src.bleed_overlay import composite_guides


@pytest.fixture
def blank_png(tmp_path: Path) -> Path:
    """600x400 white PNG simulating a 6×4 in soft-proof at 100 dpi."""
    p = tmp_path / "soft_proof.png"
    Image.new("RGB", (600, 400), (255, 255, 255)).save(p, format="PNG")
    return p


def test_returns_path(blank_png: Path):
    out = composite_guides(
        blank_png, trim_w_in=6.0, trim_h_in=4.0,
        bleed_in=0.0, safety_in=0.1875, dpi=100,
    )
    assert out == blank_png
    assert blank_png.is_file()


def test_trim_pixels_changed_no_bleed(blank_png: Path):
    """With no bleed, trim line sits at the image edge — top row should change."""
    composite_guides(
        blank_png, trim_w_in=6.0, trim_h_in=4.0,
        bleed_in=0.0, safety_in=0.1875, dpi=100,
    )
    img = Image.open(blank_png).convert("RGB")
    # Top-left corner should now have the red trim color (or close to it).
    px = img.getpixel((0, 0))
    assert px != (255, 255, 255)


def test_bleed_inset(tmp_path: Path):
    """With bleed > 0, the trim line is inset by bleed_in × dpi pixels."""
    # Trim 5×5 in + 0.125 in bleed each side = 5.25 × 5.25 image at 100 dpi
    # → 525×525 pixels. Trim should sit at pixel 12 (round(0.125*100)).
    p = tmp_path / "bleed.png"
    Image.new("RGB", (525, 525), (255, 255, 255)).save(p, format="PNG")
    composite_guides(
        p, trim_w_in=5.0, trim_h_in=5.0,
        bleed_in=0.125, safety_in=0.0, dpi=100,
    )
    img = Image.open(p).convert("RGB")
    # Pixel at the very corner should be bleed (magenta-ish, dashed) — could
    # be white if it falls in a dash gap; instead test that at LEAST one
    # corner-region pixel has changed.
    edge_changed = any(
        img.getpixel((x, 0)) != (255, 255, 255)
        for x in range(0, 525, 1)
    )
    assert edge_changed, "no bleed line drawn at top edge"
    # Pixel at trim line (row 12) should have the red trim color.
    trim_row = [img.getpixel((x, 12)) for x in range(13, 512)]
    assert any(p[0] > 200 and p[1] < 80 for p in trim_row), \
        "no red trim line at expected inset"


def test_safety_too_large_no_crash(blank_png: Path):
    """Safety bigger than half the trim should not crash."""
    composite_guides(
        blank_png, trim_w_in=6.0, trim_h_in=4.0,
        bleed_in=0.0, safety_in=10.0, dpi=100,
    )


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        composite_guides(
            tmp_path / "nope.png",
            trim_w_in=1.0, trim_h_in=1.0,
            bleed_in=0.0, safety_in=0.0, dpi=100,
        )
