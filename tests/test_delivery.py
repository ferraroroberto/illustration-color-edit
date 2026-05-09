"""Tests for src.delivery snapshot creation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.delivery import create_snapshot


def _seed_project(tmp_path: Path) -> tuple[Path, Path]:
    """Create a fake project root with config files + an output dir with PDFs."""
    proj = tmp_path / "proj"
    out = proj / "output_cmyk"
    out.mkdir(parents=True)
    # Fake configs at project root.
    (proj / "config.json").write_text('{"a":1}\n', encoding="utf-8")
    (proj / "color-config.json").write_text('{"b":2}\n', encoding="utf-8")
    # Two fake PDFs.
    (out / "fig01_CMYK.pdf").write_bytes(b"%PDF-1.4 fake one")
    (out / "fig02_CMYK.pdf").write_bytes(b"%PDF-1.4 fake two")
    # And a stray non-CMYK file that must be ignored by the default glob.
    (out / "stray.txt").write_bytes(b"ignored")
    return proj, out


def test_creates_directory_with_manifest_and_readme(tmp_path: Path):
    proj, out = _seed_project(tmp_path)
    target = create_snapshot(
        label="acme-2026",
        project_root=proj,
        output_dir=out,
        icc_profile="/tmp/test.icc",
        pdfx=False,
        width_inches=5.5,
        height_inches=7.5,
    )
    assert target.is_dir()
    assert (target / "manifest.json").is_file()
    assert (target / "README.md").is_file()
    assert (target / "config.json.snapshot").is_file()
    assert (target / "color-config.json.snapshot").is_file()


def test_manifest_lists_each_pdf(tmp_path: Path):
    proj, out = _seed_project(tmp_path)
    target = create_snapshot(
        label="run", project_root=proj, output_dir=out,
    )
    raw = json.loads((target / "manifest.json").read_text())
    names = sorted(f["source_filename"] for f in raw["files"])
    assert names == ["fig01_CMYK.pdf", "fig02_CMYK.pdf"]
    # SHA256 is deterministic — the bytes are tiny known strings.
    for entry in raw["files"]:
        assert len(entry["sha256"]) == 64


def test_pdfs_dir_has_copies(tmp_path: Path):
    proj, out = _seed_project(tmp_path)
    target = create_snapshot(
        label="run", project_root=proj, output_dir=out,
    )
    pdfs_dir = target / "pdfs"
    assert pdfs_dir.is_dir()
    assert (pdfs_dir / "fig01_CMYK.pdf").is_file()
    assert (pdfs_dir / "fig02_CMYK.pdf").is_file()
    # The non-PDF stray file should NOT be picked up.
    assert not (pdfs_dir / "stray.txt").exists()


def test_label_is_required(tmp_path: Path):
    proj, out = _seed_project(tmp_path)
    with pytest.raises(ValueError):
        create_snapshot(
            label="   ", project_root=proj, output_dir=out,
        )


def test_two_snapshots_dont_collide(tmp_path: Path):
    proj, out = _seed_project(tmp_path)
    a = create_snapshot(label="run", project_root=proj, output_dir=out)
    # Force a one-second gap by manipulating mtime; deliveries embed a
    # full HHMMSS so two snapshots in the same second would collide
    # (acceptable failure — the user shouldn't be doing that).
    import time as _time
    _time.sleep(1.0)
    b = create_snapshot(label="run", project_root=proj, output_dir=out)
    assert a != b
    assert a.is_dir() and b.is_dir()


def test_missing_semantic_palette_does_not_break(tmp_path: Path):
    proj, out = _seed_project(tmp_path)
    # No semantic-palette.json exists — snapshot should still succeed.
    target = create_snapshot(label="ok", project_root=proj, output_dir=out)
    assert not (target / "semantic-palette.json.snapshot").exists()


def test_includes_semantic_palette_when_present(tmp_path: Path):
    proj, out = _seed_project(tmp_path)
    (proj / "semantic-palette.json").write_text('{"slots":{}}', encoding="utf-8")
    target = create_snapshot(label="ok", project_root=proj, output_dir=out)
    assert (target / "semantic-palette.json.snapshot").is_file()
