"""Tests for src.colorblind."""

from __future__ import annotations

from src.colorblind import (
    CB_TYPES,
    assess_risk,
    simulate_hex,
    simulate_mapping,
)


def test_normal_is_passthrough():
    assert simulate_hex("#E74C3C", "normal") == "#E74C3C"


def test_severity_zero_is_identity():
    for cb in CB_TYPES:
        assert simulate_hex("#E74C3C", cb, severity=0.0) == "#E74C3C"


def test_achromat_full_collapses_to_gray():
    # Full achromat: red and green should both look like the same gray
    # (their luma differs but is still a single channel — no chroma left).
    sim_red = simulate_hex("#FF0000", "achromat", severity=1.0)
    sim_green = simulate_hex("#00FF00", "achromat", severity=1.0)
    # Both are pure grays now (R == G == B).
    assert sim_red[1:3] == sim_red[3:5] == sim_red[5:7]
    assert sim_green[1:3] == sim_green[3:5] == sim_green[5:7]


def test_deutan_collapses_red_green():
    # The whole point of deutan: red and green should become much closer.
    from src.cmyk_gamut import _hex_to_rgb, delta_e_76
    a, b = "#E74C3C", "#46AA3A"
    orig = delta_e_76(_hex_to_rgb(a), _hex_to_rgb(b))
    sim_a = simulate_hex(a, "deutan")
    sim_b = simulate_hex(b, "deutan")
    sim = delta_e_76(_hex_to_rgb(sim_a), _hex_to_rgb(sim_b))
    assert sim < orig, "deutan should reduce red-green distance"


def test_simulate_mapping_skips_identity():
    # Black under any sim is still black-ish — sometimes literally identical.
    sim_map = simulate_mapping(["#000000", "#FF0000"], "deutan")
    # #FF0000 should remap; #000000 might be identity.
    assert "#FF0000" in sim_map


def test_assess_risk_flags_red_green_pair():
    # Red and green are clearly distinct in the original (high ΔE) but
    # collapse under deutan/protan.
    risk = assess_risk(["#E74C3C", "#46AA3A"])
    assert risk.deutan or risk.protan, \
        "red/green palette should trigger at least one CB risk"
    assert risk.any_affected


def test_assess_risk_no_collapse_for_blue_yellow():
    # Blue/yellow distinguishable under all common deficiencies.
    risk = assess_risk(["#0066CC", "#FFCC00"])
    assert not risk.deutan
    assert not risk.protan


def test_assess_risk_empty_palette():
    risk = assess_risk([])
    assert not risk.any_affected
    assert risk.collapsed_pairs == []
