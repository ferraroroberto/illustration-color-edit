"""Tests for src.palette and src.palette_store.

Covers the palette core: clustering determinism, hue-family bucketing,
merge/delete invariants, nearest-swatch lookup, JSON roundtrip, and
the ICC-signature helper. The new ``cmyk_roundtrip_rgb`` helper is
exercised against the project's own SWOP profile when present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.cmyk_gamut import cmyk_roundtrip_rgb
from src.palette import (
    HUE_FAMILIES,
    Palette,
    Swatch,
    bucketize_for_grid,
    hue_family,
    nearest_swatch,
    seed_from_hexes,
)
from src.palette_store import PaletteStore, make_icc_signature

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SWOP_PROFILE = PROJECT_ROOT / "profiles" / "USWebCoatedSWOP.icc"


# --------------------------------------------------------------------------- #
# Swatch
# --------------------------------------------------------------------------- #
def test_swatch_normalizes_hex_and_dedupes_members():
    sw = Swatch(
        id="p_001",
        source_hex="#aabbcc",
        members=["#FF0000", "#ff0000", "#00ff00"],
    )
    assert sw.source_hex == "#AABBCC"
    assert sw.members == ["#00FF00", "#FF0000"]


def test_swatch_dict_roundtrip():
    sw = Swatch(id="p_007", source_hex="#123456", label="indigo", members=["#123456"])
    raw = sw.to_dict()
    sw2 = Swatch.from_dict(raw)
    assert sw2 == sw


# --------------------------------------------------------------------------- #
# Palette mutation
# --------------------------------------------------------------------------- #
def _palette_with(*hexes: str) -> Palette:
    p = Palette()
    for h in hexes:
        p.add(h)
    return p


def test_palette_add_assigns_unique_ids():
    p = _palette_with("#FF0000", "#00FF00", "#0000FF")
    ids = [s.id for s in p]
    assert ids == ["p_001", "p_002", "p_003"]
    assert len(set(ids)) == 3


def test_palette_delete_removes_and_clears_appearance_cache():
    p = _palette_with("#FF0000", "#00FF00")
    p.appearance_cache["p_001"] = "#AA0000"
    assert p.delete("p_001") is True
    assert p.find("p_001") is None
    assert "p_001" not in p.appearance_cache
    assert p.delete("p_001") is False


def test_palette_merge_combines_members_and_drops_other():
    p = Palette()
    a = p.add("#FF0000")
    b = p.add("#FF1010")
    a.members = ["#FF0000", "#FF1111"]
    b.members = ["#FF1010", "#FF2222"]
    merged = p.merge(a.id, b.id)
    assert merged.id == a.id
    assert merged.source_hex == "#FF0000"  # target's source preserved
    assert set(merged.members) == {"#FF0000", "#FF1010", "#FF1111", "#FF2222"}
    assert p.find(b.id) is None


def test_palette_merge_self_raises():
    p = _palette_with("#FF0000")
    with pytest.raises(ValueError):
        p.merge("p_001", "p_001")


def test_palette_merge_unknown_raises():
    p = _palette_with("#FF0000")
    with pytest.raises(KeyError):
        p.merge("p_001", "p_999")


def test_palette_next_id_after_deletions():
    p = _palette_with("#FF0000", "#00FF00", "#0000FF")
    p.delete("p_002")
    # next_id should fill the gap, not extend past the highest used.
    assert p.next_id() == "p_002"


def test_palette_dict_roundtrip_preserves_appearance_cache():
    p = _palette_with("#FF0000", "#00FF00")
    p.icc_signature = "/some/path::123.456"
    p.appearance_cache = {"p_001": "#CC1010", "p_002": "#10AA10"}
    raw = p.to_dict()
    p2 = Palette.from_dict(raw)
    assert p2.icc_signature == p.icc_signature
    assert p2.appearance_cache == p.appearance_cache
    assert [s.id for s in p2] == [s.id for s in p]


def test_palette_is_appearance_fresh():
    p = Palette(icc_signature="sig-A")
    assert p.is_appearance_fresh("sig-A")
    assert not p.is_appearance_fresh("sig-B")
    assert not p.is_appearance_fresh("")


# --------------------------------------------------------------------------- #
# Hue families
# --------------------------------------------------------------------------- #
def test_hue_family_classifications():
    # Pure primaries.
    assert hue_family("#FF0000") == "red"
    assert hue_family("#00FF00") == "green"
    assert hue_family("#0000FF") == "blue"
    # Yellow/orange/cyan/purple bands.
    assert hue_family("#FFFF00") == "yellow"
    assert hue_family("#FF8800") == "orange"
    assert hue_family("#00FFFF") in {"cyan", "green"}  # right at the boundary
    assert hue_family("#8000FF") == "purple"


def test_hue_family_low_chroma_is_neutral():
    for gray in ("#000000", "#404040", "#808080", "#C0C0C0", "#FFFFFF"):
        assert hue_family(gray) == "neutral", gray
    # Slightly tinted grays should still classify as neutral.
    assert hue_family("#807F7F") == "neutral"


# --------------------------------------------------------------------------- #
# bucketize_for_grid
# --------------------------------------------------------------------------- #
def test_bucketize_returns_all_families_in_order():
    grid = bucketize_for_grid([])
    assert list(grid.keys()) == list(HUE_FAMILIES)
    assert all(len(row) == 6 for row in grid.values())
    assert all(cell is None for row in grid.values() for cell in row)


def test_bucketize_places_swatches_by_family_and_lightness():
    p = Palette()
    dark_red = p.add("#330000")
    light_red = p.add("#FFCCCC")
    grid = bucketize_for_grid(p.swatches)
    red_row = grid["red"]
    # The dark and light reds should land in different cells, with the
    # dark one earlier (lower L → lower index).
    placed = [(i, sw) for i, sw in enumerate(red_row) if sw is not None]
    assert len(placed) == 2
    assert placed[0][1] is dark_red
    assert placed[1][1] is light_red
    assert placed[0][0] < placed[1][0]


def test_bucketize_never_drops_a_swatch():
    # Force a collision: many similar mid-lightness reds.
    hexes = [f"#A{i:02X}A{i:02X}" for i in range(10)]
    p = Palette()
    for h in hexes:
        p.add(h)
    grid = bucketize_for_grid(p.swatches)
    placed_count = sum(1 for row in grid.values() for cell in row if cell is not None)
    assert placed_count == len(hexes)


def test_bucketize_rejects_too_few_bins():
    with pytest.raises(ValueError):
        bucketize_for_grid([], lightness_bins=1)


# --------------------------------------------------------------------------- #
# nearest_swatch
# --------------------------------------------------------------------------- #
def test_nearest_swatch_finds_closest_in_lab():
    p = _palette_with("#FF0000", "#00FF00", "#0000FF")
    near_red = nearest_swatch("#EE2222", p.swatches)
    assert near_red is not None
    assert near_red.source_hex == "#FF0000"
    near_green = nearest_swatch("#10DD20", p.swatches)
    assert near_green is not None
    assert near_green.source_hex == "#00FF00"


def test_nearest_swatch_empty_palette_returns_none():
    assert nearest_swatch("#FF0000", []) is None


# --------------------------------------------------------------------------- #
# K-means seeding
# --------------------------------------------------------------------------- #
def test_seed_empty_input_is_empty():
    assert seed_from_hexes([], 5) == []


def test_seed_single_color_one_swatch():
    swatches = seed_from_hexes(["#FF0000"], 1)
    assert len(swatches) == 1
    assert swatches[0].source_hex == "#FF0000"
    assert swatches[0].members == ["#FF0000"]


def test_seed_k_larger_than_inputs_returns_one_swatch_per_input():
    inputs = ["#FF0000", "#00FF00", "#0000FF"]
    swatches = seed_from_hexes(inputs, 10)
    assert len(swatches) == 3
    assert {s.source_hex for s in swatches} == set(inputs)
    # Every input ends up as a member of exactly one swatch.
    all_members = [m for s in swatches for m in s.members]
    assert sorted(all_members) == sorted(inputs)


def test_seed_is_deterministic_for_same_input():
    inputs = [
        "#E74C3C", "#D63E2E", "#E0584A",          # reds
        "#3498DB", "#2980B9", "#5DADE2",          # blues
        "#2ECC71", "#27AE60", "#52BE80",          # greens
        "#F1C40F", "#F39C12",                     # yellows
    ]
    a = seed_from_hexes(inputs, 4)
    b = seed_from_hexes(inputs, 4)
    assert [(s.id, s.source_hex, s.members) for s in a] == \
           [(s.id, s.source_hex, s.members) for s in b]


def test_seed_input_order_does_not_change_result():
    inputs = ["#E74C3C", "#3498DB", "#2ECC71", "#F1C40F"]
    a = seed_from_hexes(inputs, 3)
    b = seed_from_hexes(list(reversed(inputs)), 3)
    assert [(s.source_hex, s.members) for s in a] == \
           [(s.source_hex, s.members) for s in b]


def test_seed_different_input_can_recluster():
    base = ["#E74C3C", "#3498DB", "#2ECC71", "#F1C40F", "#9B59B6"]
    extended = base + ["#1ABC9C", "#E67E22", "#34495E"]
    a = seed_from_hexes(base, 3)
    b = seed_from_hexes(extended, 3)
    # Hard guarantee: at least one swatch differs (membership grew).
    a_members = {tuple(s.members) for s in a}
    b_members = {tuple(s.members) for s in b}
    assert a_members != b_members


def test_seed_clusters_perceptually_similar_colors_together():
    # Three tight reds + three tight blues. With k=2 they should split
    # cleanly into red cluster and blue cluster.
    reds = ["#E74C3C", "#D63E2E", "#E0584A"]
    blues = ["#3498DB", "#2980B9", "#5DADE2"]
    swatches = seed_from_hexes(reds + blues, 2)
    assert len(swatches) == 2
    member_sets = [set(s.members) for s in swatches]
    assert set(reds) in member_sets
    assert set(blues) in member_sets


# --------------------------------------------------------------------------- #
# PaletteStore + ICC signature
# --------------------------------------------------------------------------- #
def test_palette_store_save_load_roundtrip(tmp_path):
    palette_path = tmp_path / "palette.json"
    store = PaletteStore(palette_path)

    p = Palette()
    p.add("#FF0000", label="red")
    p.add("#0000FF", label="blue")
    p.icc_signature = "fake::1.0"
    p.appearance_cache = {"p_001": "#CC1010"}
    store.save(p)

    loaded = store.load()
    assert [s.source_hex for s in loaded] == ["#FF0000", "#0000FF"]
    assert loaded.icc_signature == "fake::1.0"
    assert loaded.appearance_cache == {"p_001": "#CC1010"}


def test_palette_store_load_missing_returns_empty(tmp_path):
    store = PaletteStore(tmp_path / "does_not_exist.json")
    p = store.load()
    assert isinstance(p, Palette)
    assert len(p) == 0


def test_palette_store_load_corrupt_returns_empty(tmp_path):
    bad = tmp_path / "palette.json"
    bad.write_text("{not valid json", encoding="utf-8")
    p = PaletteStore(bad).load()
    assert isinstance(p, Palette)
    assert len(p) == 0


def test_palette_store_delete(tmp_path):
    store = PaletteStore(tmp_path / "palette.json")
    store.save(Palette())
    assert store.delete() is True
    assert store.delete() is False


def test_make_icc_signature_for_missing_path_is_empty(tmp_path):
    assert make_icc_signature(tmp_path / "missing.icc") == ""


def test_make_icc_signature_changes_with_mtime(tmp_path):
    fake = tmp_path / "fake.icc"
    fake.write_bytes(b"fake profile")
    sig1 = make_icc_signature(fake)
    assert sig1
    # Touch with a future mtime → signature changes.
    import os
    os.utime(fake, (1, 1))
    sig2 = make_icc_signature(fake)
    assert sig1 != sig2


# --------------------------------------------------------------------------- #
# cmyk_roundtrip_rgb (only runs if the SWOP profile is present)
# --------------------------------------------------------------------------- #
needs_swop = pytest.mark.skipif(
    not SWOP_PROFILE.is_file(),
    reason=f"SWOP profile not present at {SWOP_PROFILE}",
)


def test_cmyk_roundtrip_returns_none_for_missing_profile(tmp_path):
    assert cmyk_roundtrip_rgb("#FF0000", tmp_path / "missing.icc") is None


@needs_swop
def test_cmyk_roundtrip_returns_uppercase_hex():
    out = cmyk_roundtrip_rgb("#FF0000", SWOP_PROFILE)
    assert out is not None
    assert out.startswith("#") and len(out) == 7
    assert out == out.upper()


@needs_swop
def test_cmyk_roundtrip_white_stays_near_white():
    out = cmyk_roundtrip_rgb("#FFFFFF", SWOP_PROFILE)
    assert out is not None
    r = int(out[1:3], 16)
    g = int(out[3:5], 16)
    b = int(out[5:7], 16)
    # Paper white roundtrips close to white (small ink shift is OK).
    assert min(r, g, b) > 230
