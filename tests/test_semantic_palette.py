"""Tests for src.semantic_palette."""

from __future__ import annotations

from pathlib import Path

from src.semantic_palette import (
    SemanticPalette,
    SemanticPaletteStore,
    auto_migrate_global_map,
    merge_with_semantic,
)


class TestSemanticPaletteResolution:
    def test_slot_for_hex_case_insensitive(self):
        p = SemanticPalette()
        p.upsert_slot("status.bad", "#E74C3C", label="Bad")
        assert p.slot_for_hex("#e74c3c").name == "status.bad"
        assert p.slot_for_hex("#FFFFFF") is None

    def test_resolve_returns_active_theme_target(self):
        p = SemanticPalette()
        p.upsert_slot("status.bad", "#E74C3C")
        p.set_theme_target("status.bad", "cmyk", "#D14B3C")
        p.set_theme_target("status.bad", "grayscale", "#373737")
        assert p.resolve("#E74C3C", "cmyk") == "#D14B3C"
        assert p.resolve("#E74C3C", "grayscale") == "#373737"

    def test_resolve_none_when_slot_missing_in_theme(self):
        p = SemanticPalette()
        p.upsert_slot("status.bad", "#E74C3C")
        assert p.resolve("#E74C3C", "cmyk") is None

    def test_clear_theme_target(self):
        p = SemanticPalette()
        p.upsert_slot("status.bad", "#E74C3C")
        p.set_theme_target("status.bad", "cmyk", "#D14B3C")
        assert p.resolve("#E74C3C", "cmyk") == "#D14B3C"
        assert p.clear_theme_target("status.bad", "cmyk") is True
        assert p.resolve("#E74C3C", "cmyk") is None

    def test_remove_slot_strips_from_themes(self):
        p = SemanticPalette()
        p.upsert_slot("status.bad", "#E74C3C")
        p.set_theme_target("status.bad", "cmyk", "#D14B3C")
        p.set_theme_target("status.bad", "grayscale", "#373737")
        p.remove_slot("status.bad")
        # Both buckets should have purged the slot.
        assert "status.bad" not in p.themes["default"].cmyk
        assert "status.bad" not in p.themes["default"].grayscale


class TestMergeWithSemantic:
    def test_pure_global_when_no_semantic(self):
        gm = {"#E74C3C": {"target": "#373737", "label": "", "notes": ""}}
        merged = merge_with_semantic(gm, {}, None, "grayscale")
        assert merged == {"#E74C3C": "#373737"}

    def test_semantic_overrides_global(self):
        gm = {"#E74C3C": {"target": "#000000", "label": "", "notes": ""}}
        p = SemanticPalette()
        p.upsert_slot("status.bad", "#E74C3C")
        p.set_theme_target("status.bad", "grayscale", "#373737")
        merged = merge_with_semantic(gm, {}, p, "grayscale")
        # Semantic theme target wins over the legacy global hex target.
        assert merged["#E74C3C"] == "#373737"

    def test_per_file_override_wins_over_semantic(self):
        gm = {}
        p = SemanticPalette()
        p.upsert_slot("status.bad", "#E74C3C")
        p.set_theme_target("status.bad", "cmyk", "#D14B3C")
        merged = merge_with_semantic(
            gm, {"#E74C3C": "#FF8800"}, p, "cmyk",
        )
        assert merged["#E74C3C"] == "#FF8800"

    def test_unrelated_hex_passes_through(self):
        gm = {}
        p = SemanticPalette()
        p.upsert_slot("status.bad", "#E74C3C")
        p.set_theme_target("status.bad", "cmyk", "#D14B3C")
        merged = merge_with_semantic(gm, {}, p, "cmyk")
        # Only the slot's authored hex is mapped — others stay empty.
        assert merged == {"#E74C3C": "#D14B3C"}


class TestAutoMigrate:
    def test_migrates_unbound_entries(self):
        p = SemanticPalette()
        gm = {
            "#E74C3C": {"target": "#373737", "label": "red", "notes": ""},
            "#46AA3A": {"target": "#E4E4E4", "label": "green", "notes": ""},
        }
        n = auto_migrate_global_map(p, gm, "grayscale")
        assert n == 2
        assert len(p.slots) == 2
        # Each authored hex now resolves through the auto-named slot.
        assert p.resolve("#E74C3C", "grayscale") == "#373737"
        assert p.resolve("#46AA3A", "grayscale") == "#E4E4E4"

    def test_idempotent(self):
        p = SemanticPalette()
        gm = {"#E74C3C": {"target": "#373737", "label": "", "notes": ""}}
        n1 = auto_migrate_global_map(p, gm, "grayscale")
        n2 = auto_migrate_global_map(p, gm, "grayscale")
        assert n1 == 1
        assert n2 == 0
        assert len(p.slots) == 1

    def test_existing_slot_skipped(self):
        p = SemanticPalette()
        p.upsert_slot("status.bad", "#E74C3C", label="Bad")
        gm = {"#E74C3C": {"target": "#373737", "label": "", "notes": ""}}
        n = auto_migrate_global_map(p, gm, "grayscale")
        # Pre-existing slot claims the hex — auto-migrate adds nothing.
        assert n == 0
        assert "status.bad" in p.slots


class TestStorePersistence:
    def test_roundtrip(self, tmp_path: Path):
        path = tmp_path / "semantic-palette.json"
        store = SemanticPaletteStore(path)
        p = SemanticPalette()
        p.upsert_slot("status.bad", "#E74C3C", label="Bad", notes="negative")
        p.set_theme_target("status.bad", "cmyk", "#D14B3C")
        p.set_theme_target("status.bad", "grayscale", "#373737")
        store.save(p)

        loaded = store.load()
        assert "status.bad" in loaded.slots
        assert loaded.slots["status.bad"].authored == "#E74C3C"
        assert loaded.resolve("#E74C3C", "cmyk") == "#D14B3C"
        assert loaded.resolve("#E74C3C", "grayscale") == "#373737"

    def test_missing_file_returns_empty(self, tmp_path: Path):
        store = SemanticPaletteStore(tmp_path / "nonexistent.json")
        loaded = store.load()
        assert loaded.slots == {}
