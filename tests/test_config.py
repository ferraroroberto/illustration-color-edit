"""Tests for src.config — CmykExportConfig save/load round trip.

Guards the invariant described in issue #33: every field on
``CmykExportConfig`` must survive ``to_json()`` -> ``_build_cmyk_export_config()``
unchanged. Adding a new dataclass field without wiring it through ``to_json``
(or the loader) should make this test fail loudly rather than silently
dropping the setting from ``config.json`` on save.
"""

from __future__ import annotations

from pathlib import Path

from src.config import CmykExportConfig, _build_cmyk_export_config


def _non_default_cmyk_export_config(tmp_path: Path) -> CmykExportConfig:
    """A CmykExportConfig with every field set away from its default.

    Using non-default values (rather than ``CmykExportConfig()``) makes the
    round-trip assertion meaningful — a field silently dropped on save would
    reload as its default and this would still equal a *default* instance,
    masking the bug. Path fields are anchored under ``tmp_path`` so they are
    genuinely absolute (and OS-native) rather than relying on a POSIX-only
    literal like ``/tmp/...``, which ``pathlib`` does not treat as absolute
    on Windows.
    """
    return CmykExportConfig(
        enabled=False,
        output_dir=tmp_path / "cmyk_out",
        icc_profile_path=tmp_path / "profiles" / "custom.icc",
        ghostscript_path="/usr/bin/gs",
        target_width_inches=6.125,
        target_height_inches=9.25,
        bleed_inches=0.125,
        pdfx_compliance="PDF/X-4",
        generate_preview_png=False,
        preview_dpi=200,
        audit_artifacts=False,
        filename_template="fig_{chapter:02d}_{figure:02d}_CMYK",
        tac_limit_percent=280.0,
        tac_check_dpi=150,
        force_k_min_stroke_pt=0.75,
        force_k_min_text_pt=10.0,
        safety_inches=0.25,
        show_guide_overlay=False,
        trim_to_content_enabled=True,
        trim_to_content_padding_pt=12.0,
        print_subdir="print_custom",
        preview_subdir="preview_custom",
        generate_full_preview=False,
        render_check=False,
        render_check_dpi=150,
    )


def test_cmyk_export_config_round_trips_through_to_json(tmp_path: Path) -> None:
    original = _non_default_cmyk_export_config(tmp_path)

    raw = original.to_json()
    # base is irrelevant here since output_dir/icc_profile_path are absolute,
    # but _build_cmyk_export_config still requires a Path.
    reloaded = _build_cmyk_export_config(raw, base=tmp_path)

    assert reloaded == original


def test_cmyk_export_config_to_json_nests_trim_to_content_and_subdirs(tmp_path: Path) -> None:
    cfg = _non_default_cmyk_export_config(tmp_path)
    raw = cfg.to_json()

    assert raw["trim_to_content"] == {
        "enabled": cfg.trim_to_content_enabled,
        "padding_pt": cfg.trim_to_content_padding_pt,
    }
    assert raw["subdirs"] == {
        "print": cfg.print_subdir,
        "preview": cfg.preview_subdir,
    }
    # Flattened attribute names never leak into the JSON shape.
    assert "trim_to_content_enabled" not in raw
    assert "trim_to_content_padding_pt" not in raw
    assert "print_subdir" not in raw
    assert "preview_subdir" not in raw


def test_cmyk_export_config_to_json_stringifies_paths(tmp_path: Path) -> None:
    cfg = _non_default_cmyk_export_config(tmp_path)
    raw = cfg.to_json()

    assert raw["output_dir"] == str(cfg.output_dir)
    assert raw["icc_profile_path"] == str(cfg.icc_profile_path)
    assert isinstance(raw["output_dir"], str)
    assert isinstance(raw["icc_profile_path"], str)
