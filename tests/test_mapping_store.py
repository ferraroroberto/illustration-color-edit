"""Tests for src.mapping_store."""

from __future__ import annotations

import json

import pytest

from src.mapping_store import (
    IllustrationMapping,
    MappingStore,
    VALID_STATUSES,
    merge_mappings,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "global_color_map": {
            "#E74C3C": {"target": "#333333", "label": "red / bad", "notes": ""},
        },
        "matching": {"nearest_enabled": True, "metric": "lab", "threshold": 10.0},
        "paths": {"input_dir": "./input", "output_dir": "./output", "metadata_dir": "./metadata"},
    }), encoding="utf-8")
    meta = tmp_path / "metadata"
    meta.mkdir()
    return MappingStore(config_path=cfg, metadata_dir=meta)


# --------------------------------------------------------------------------- #
# IllustrationMapping
# --------------------------------------------------------------------------- #
def test_illustration_mapping_roundtrip():
    m = IllustrationMapping(
        filename="x.svg",
        status="reviewed",
        overrides={"#ff0000": "#222222"},
        notes="hello",
    )
    d = m.to_dict()
    assert d["overrides"] == {"#FF0000": "#222222"}  # normalized
    m2 = IllustrationMapping.from_dict(d)
    assert m2.filename == "x.svg"
    assert m2.status == "reviewed"
    assert m2.overrides == {"#FF0000": "#222222"}


def test_illustration_mapping_with_status_validates():
    m = IllustrationMapping(filename="x.svg")
    m.with_status("reviewed")
    assert m.status == "reviewed"
    assert m.updated_at  # timestamp set
    with pytest.raises(ValueError):
        m.with_status("garbage")  # type: ignore[arg-type]


def test_illustration_mapping_unknown_status_coerces_to_pending():
    m = IllustrationMapping.from_dict({"filename": "x.svg", "status": "weird"})
    assert m.status == "pending"


# --------------------------------------------------------------------------- #
# Global map IO
# --------------------------------------------------------------------------- #
def test_load_global_map_returns_canonical_keys(store):
    gm = store.load_global_map()
    assert "#E74C3C" in gm
    assert gm["#E74C3C"]["target"] == "#333333"


def test_save_global_map_preserves_other_config_sections(store):
    store.save_global_map({"#FF0000": {"target": "#111111", "label": "x", "notes": ""}})
    raw = json.loads(store.config_path.read_text(encoding="utf-8"))
    assert "matching" in raw  # untouched
    assert raw["global_color_map"] == {
        "#FF0000": {"target": "#111111", "label": "x", "notes": ""}
    }


def test_upsert_and_remove_global_entry(store):
    store.upsert_global_entry("#aabbcc", "#222222", label="lbl", notes="nts")
    gm = store.load_global_map()
    assert gm["#AABBCC"]["target"] == "#222222"
    assert gm["#AABBCC"]["label"] == "lbl"

    assert store.remove_global_entry("#AABBCC") is True
    assert "#AABBCC" not in store.load_global_map()
    # Removing a non-existent entry is a no-op.
    assert store.remove_global_entry("#AABBCC") is False


# --------------------------------------------------------------------------- #
# Per-illustration IO
# --------------------------------------------------------------------------- #
def test_load_illustration_returns_pending_when_missing(store):
    m = store.load_illustration("nope.svg")
    assert m.status == "pending"
    assert m.overrides == {}
    # Should not have written anything to disk.
    assert not store.metadata_path_for("nope.svg").exists()


def test_save_then_load_illustration(store):
    m = IllustrationMapping(filename="figure-01.svg")
    m.overrides = {"#E74C3C": "#333333"}
    m.with_status("reviewed")
    store.save_illustration(m)

    again = store.load_illustration("figure-01.svg")
    assert again.status == "reviewed"
    assert again.overrides == {"#E74C3C": "#333333"}
    assert again.updated_at == m.updated_at


def test_save_illustration_uses_basename_only(store):
    m = IllustrationMapping(filename="subdir/x.svg", overrides={"#FF0000": "#000000"})
    m.filename = "x.svg"  # caller passes basename
    store.save_illustration(m)

    # The on-disk file is at metadata/<base>.mapping.json
    assert (store.metadata_dir / "x.svg.mapping.json").exists()


def test_delete_illustration(store):
    m = IllustrationMapping(filename="x.svg")
    store.save_illustration(m)
    assert store.delete_illustration("x.svg") is True
    assert store.delete_illustration("x.svg") is False


def test_load_illustration_handles_corrupt_metadata(store, caplog):
    p = store.metadata_path_for("bad.svg")
    p.write_text("not-json{", encoding="utf-8")
    m = store.load_illustration("bad.svg")
    assert m.status == "pending"
    assert m.overrides == {}


# --------------------------------------------------------------------------- #
# all_illustrations + history
# --------------------------------------------------------------------------- #
def _save(store, name, overrides):
    m = IllustrationMapping(filename=name, overrides=overrides)
    store.save_illustration(m)


def test_all_illustrations_lists_metadata_files(store):
    _save(store, "a.svg", {"#FF0000": "#111111"})
    _save(store, "b.svg", {"#00FF00": "#222222"})
    out = sorted(m.filename for m in store.all_illustrations())
    assert out == ["a.svg", "b.svg"]


def test_history_aggregates_across_illustrations_and_global(store):
    _save(store, "a.svg", {"#FF0000": "#111111"})
    _save(store, "b.svg", {"#FF0000": "#111111"})
    _save(store, "c.svg", {"#FF0000": "#222222"})

    hist = store.history()
    # Global map contributes its #E74C3C->#333333.
    assert hist["#E74C3C"] == {"#333333": 1}
    # Three illustrations: two map red to #111111, one to #222222.
    assert hist["#FF0000"] == {"#111111": 2, "#222222": 1}


def test_history_used_by_suggest_from_history(store):
    from src.color_mapper import suggest_from_history
    _save(store, "a.svg", {"#FF0000": "#111111"})
    _save(store, "b.svg", {"#FF0000": "#111111"})
    out = suggest_from_history("#FF0000", store.history())
    assert out[0] == ("#111111", 2)


# --------------------------------------------------------------------------- #
# merge_mappings
# --------------------------------------------------------------------------- #
def test_merge_mappings_overrides_win():
    gm = {
        "#FF0000": {"target": "#111111", "label": "r"},
        "#00FF00": {"target": "#222222", "label": "g"},
    }
    over = {"#ff0000": "#999999"}
    merged = merge_mappings(gm, over)
    assert merged["#FF0000"] == "#999999"
    assert merged["#00FF00"] == "#222222"


def test_merge_mappings_skips_global_entries_without_target():
    gm = {"#FF0000": {"target": "", "label": ""}}
    assert merge_mappings(gm, {}) == {}


def test_valid_statuses_constant_matches_literal():
    assert set(VALID_STATUSES) == {"pending", "in_progress", "reviewed", "exported"}
