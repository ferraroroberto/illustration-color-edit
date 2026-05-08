"""Tests for the CMYK extensions on src.mapping_store.

Mirrors the existing grayscale tests in test_mapping_store.py — the two
pipelines share the same persistence layer, so symmetry is the contract.
"""

from __future__ import annotations

import json

import pytest

from src.mapping_store import IllustrationMapping, MappingStore


@pytest.fixture
def store(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "global_color_map": {
                    "#E74C3C": {"target": "#333333", "label": "red / bad", "notes": ""},
                },
                "cmyk_correction_map": {
                    "#000000": {
                        "target": "#0A0A0A",
                        "label": "pure black → near-black",
                        "notes": "",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    meta = tmp_path / "metadata"
    meta.mkdir()
    return MappingStore(config_path=cfg, metadata_dir=meta)


# --------------------------------------------------------------------------- #
# IllustrationMapping carries both pipelines independently
# --------------------------------------------------------------------------- #
def test_illustration_mapping_carries_cmyk_state():
    m = IllustrationMapping(
        filename="x.svg",
        status="reviewed",
        overrides={"#ff0000": "#222222"},
        cmyk_status="pending",
        cmyk_overrides={"#000000": "#0a0a0a"},
    )
    d = m.to_dict()
    assert d["cmyk_overrides"] == {"#000000": "#0A0A0A"}
    assert d["cmyk_status"] == "pending"
    m2 = IllustrationMapping.from_dict(d)
    assert m2.status == "reviewed"
    assert m2.cmyk_status == "pending"
    assert m2.overrides == {"#FF0000": "#222222"}
    assert m2.cmyk_overrides == {"#000000": "#0A0A0A"}


def test_with_cmyk_status_independent_of_grayscale():
    m = IllustrationMapping(filename="x.svg")
    m.with_status("reviewed")
    m.with_cmyk_status("exported")
    assert m.status == "reviewed"
    assert m.cmyk_status == "exported"
    with pytest.raises(ValueError):
        m.with_cmyk_status("garbage")  # type: ignore[arg-type]


def test_legacy_metadata_without_cmyk_loads_cleanly():
    """Old metadata files have no cmyk_* keys — must default cleanly."""
    raw = {
        "filename": "x.svg",
        "status": "reviewed",
        "overrides": {"#FF0000": "#222222"},
        "updated_at": "2026-01-01T00:00:00+00:00",
        "notes": "",
    }
    m = IllustrationMapping.from_dict(raw)
    assert m.cmyk_status == "pending"
    assert m.cmyk_overrides == {}


# --------------------------------------------------------------------------- #
# Global CMYK correction map, parallel to grayscale global_color_map
# --------------------------------------------------------------------------- #
def test_load_cmyk_correction_map_normalizes(store):
    gm = store.load_cmyk_correction_map()
    assert gm == {
        "#000000": {
            "target": "#0A0A0A",
            "label": "pure black → near-black",
            "notes": "",
        }
    }


def test_upsert_remove_cmyk_correction(store):
    store.upsert_cmyk_correction_entry("#e74c3c", "#d14b3c", label="red")
    gm = store.load_cmyk_correction_map()
    assert gm["#E74C3C"]["target"] == "#D14B3C"
    assert gm["#E74C3C"]["label"] == "red"

    assert store.remove_cmyk_correction_entry("#E74C3C") is True
    assert "#E74C3C" not in store.load_cmyk_correction_map()
    assert store.remove_cmyk_correction_entry("#E74C3C") is False


def test_save_cmyk_correction_preserves_other_keys(store):
    """Saving CMYK map must not nuke the grayscale global_color_map."""
    store.save_cmyk_correction_map({"#FF00FF": {"target": "#FE00FE", "label": "", "notes": ""}})
    raw = json.loads(store.config_path.read_text(encoding="utf-8"))
    assert "global_color_map" in raw  # untouched
    assert raw["global_color_map"]["#E74C3C"]["target"] == "#333333"


# --------------------------------------------------------------------------- #
# CMYK history & usage_counts
# --------------------------------------------------------------------------- #
def test_cmyk_history_combines_global_and_per_file(store):
    a = IllustrationMapping(filename="a.svg", cmyk_overrides={"#000000": "#0A0A0A"})
    b = IllustrationMapping(filename="b.svg", cmyk_overrides={"#000000": "#0A0A0A"})
    c = IllustrationMapping(filename="c.svg", cmyk_overrides={"#000000": "#111111"})
    for m in (a, b, c):
        store.save_illustration(m)

    hist = store.cmyk_history()
    # Global contributes 1 + 3 per-file = 4 total for #000000 → either target
    assert hist["#000000"]["#0A0A0A"] == 3  # 1 global + 2 per-file
    assert hist["#000000"]["#111111"] == 1


def test_cmyk_usage_counts(store):
    a = IllustrationMapping(filename="a.svg", cmyk_overrides={"#000000": "#0A0A0A"})
    b = IllustrationMapping(filename="b.svg", cmyk_overrides={"#FF0000": "#EE0000"})
    for m in (a, b):
        store.save_illustration(m)
    counts = store.cmyk_usage_counts()
    assert counts == {"#000000": 1, "#FF0000": 1}


def test_cleanup_identity_entries_strips_global_and_per_file(store):
    # Pollute global with a real entry + an identity.
    store.upsert_cmyk_correction_entry("#FFFFFF", "#FFFFFF", label="oops identity")
    store.upsert_cmyk_correction_entry("#FF0000", "#EE0000", label="real")
    a = IllustrationMapping(
        filename="a.svg",
        cmyk_overrides={
            "#000000": "#000000",  # identity — should be stripped
            "#FF0000": "#EE0000",  # real correction
            "#888888": "#888888",  # identity
        },
    )
    b = IllustrationMapping(
        filename="b.svg",
        cmyk_overrides={"#FF0000": "#EE0000"},  # all clean
    )
    for m in (a, b):
        store.save_illustration(m)

    report = store.cleanup_identity_entries()
    assert report["global"] == 1
    assert report["files"] == 2
    assert report["metadata_files_touched"] == 1

    # Verify state on disk.
    gm = store.load_cmyk_correction_map()
    assert "#FFFFFF" not in gm
    assert "#FF0000" in gm

    a_clean = store.load_illustration("a.svg")
    assert a_clean.cmyk_overrides == {"#FF0000": "#EE0000"}
    b_clean = store.load_illustration("b.svg")
    assert b_clean.cmyk_overrides == {"#FF0000": "#EE0000"}


def test_cleanup_identity_entries_noop_when_clean(store):
    a = IllustrationMapping(
        filename="a.svg",
        cmyk_overrides={"#FF0000": "#EE0000"},
    )
    store.save_illustration(a)
    report = store.cleanup_identity_entries()
    assert report == {"global": 0, "files": 0, "metadata_files_touched": 0}
