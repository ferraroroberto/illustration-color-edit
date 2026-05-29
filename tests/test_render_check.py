"""Tests for the render-fidelity diff core (issue #8 detector).

The Inkscape/Ghostscript renders are not exercised here — those need the
real binaries and are validated manually. These tests cover the pure
:func:`find_solid_diff_regions` logic: it must flag a solid dropped shape
while ignoring the thin anti-aliasing edge differences that always exist
between two rasterisers.
"""

from __future__ import annotations

import numpy as np

from src.render_check import DiffRegion, RenderCheckReport, find_solid_diff_regions


def _blank(h: int = 120, w: int = 120) -> np.ndarray:
    """An all-white RGB canvas."""
    return np.full((h, w, 3), 255, dtype=np.uint8)


def test_identical_images_have_no_regions() -> None:
    img = _blank()
    assert find_solid_diff_regions(img, img) == []


def test_dropped_solid_block_is_flagged() -> None:
    ref = _blank()
    test = _blank()
    # A 20x20 dark block present in the reference but missing from the PDF
    # render — the signature of Inkscape dropping a stacked shape.
    ref[40:60, 40:60] = (30, 20, 25)
    regions = find_solid_diff_regions(test, ref)  # order-independent: abs diff
    assert len(regions) == 1
    r = regions[0]
    assert r.area_px >= 64
    # The region centre should sit inside the dropped block.
    cx, cy = r.center_pct(test.shape[1], test.shape[0])
    assert 35 < cx < 55 and 35 < cy < 55


def test_thin_edge_difference_is_ignored() -> None:
    # A 1-px-wide vertical line difference simulates a sub-pixel stroke
    # shift between rasterisers; erosion should erase it entirely.
    ref = _blank()
    test = _blank()
    ref[:, 60:61] = (0, 0, 0)
    assert find_solid_diff_regions(test, ref) == []


def test_mild_colour_drift_below_threshold_is_ignored() -> None:
    # A large but faint difference (well under the channel threshold) is the
    # expected rasteriser tone variance, not a dropped shape.
    ref = _blank()
    test = _blank()
    ref[20:100, 20:100] = (245, 245, 245)  # delta 10 << default threshold 96
    assert find_solid_diff_regions(test, ref) == []


def test_multiple_blocks_capped_and_sorted() -> None:
    ref = _blank(200, 200)
    test = _blank(200, 200)
    # Three blocks of different sizes; expect them returned largest-first.
    ref[10:40, 10:40] = (0, 0, 0)      # 30x30
    ref[60:100, 60:100] = (0, 0, 0)    # 40x40 (largest)
    ref[150:165, 150:165] = (0, 0, 0)  # 15x15
    regions = find_solid_diff_regions(test, ref, max_regions=2)
    assert len(regions) == 2
    assert regions[0].area_px >= regions[1].area_px


def test_report_summary_dedupes_colocated_regions() -> None:
    # A dropped dot can split into two adjacent blobs at the same rounded
    # spot; the summary should list that location once, not twice.
    r1 = DiffRegion(40, 60, 48, 70, 50)   # centre (44, 65)
    r2 = DiffRegion(39, 59, 49, 71, 45)   # centre (44, 65) — same whole-percent spot
    report = RenderCheckReport(regions=[r1, r2], page_w_px=100, page_h_px=100)
    summary = report.summary()
    assert summary.count("%,") == 1
    assert "at 1 location" in summary


def test_report_summary_empty_when_clean() -> None:
    assert RenderCheckReport().has_discrepancy is False
    assert RenderCheckReport().summary() == "no PDF render discrepancy detected"
