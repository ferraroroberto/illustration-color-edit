"""
Configuration loader for the illustration-color-edit project.

Two config files, both gitignored, with committed .example counterparts:
  config.json         — folder paths (input / output / metadata)
  color-config.json   — color mappings, matching, print-safety, logging

Resolution order for each file:
  1. <project_root>/config.json           → <project_root>/config.example.json
  2. <project_root>/color-config.json     → <project_root>/color-config.json.example
  3. built-in defaults (last resort)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class MatchingConfig:
    nearest_enabled: bool = True
    metric: str = "lab"
    threshold: float = 10.0


@dataclass
class PrintSafetyConfig:
    min_gray_value: str = "#EEEEEE"
    warn_only: bool = True


@dataclass
class PngExportConfig:
    enabled: bool = True
    dpi: int = 300
    inkscape_path: str = "inkscape"


@dataclass
class PathsConfig:
    input_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "input")
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "output")
    metadata_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "metadata")


@dataclass
class CmykExportConfig:
    """Configuration for the CMYK print export pipeline.

    Lives under ``cmyk_export`` in ``config.json`` (folder paths) and is the
    sibling of :class:`PngExportConfig` for the grayscale workflow.

    The ICC profile path and Ghostscript binary are user-supplied per machine;
    see ``docs/2026-05-07-cmyk-pipeline.md`` for sources.
    """

    enabled: bool = True
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "output_cmyk")
    icc_profile_path: Path = field(default_factory=lambda: PROJECT_ROOT / "profiles" / "ISOcoated_v2_eci.icc")
    ghostscript_path: str = "gswin64c"
    target_width_inches: float = 5.5
    target_height_inches: float = 7.5
    bleed_inches: float = 0.0
    pdfx_compliance: bool = False
    generate_preview_png: bool = True
    preview_dpi: int = 150
    audit_artifacts: bool = True
    """Keep human-inspectable companion files next to each CMYK PDF.

    When True, the pipeline writes ``<stem>_CMYK_report.txt`` (and, in PDF/X
    mode, retains ``<stem>_CMYK.pdfx_def.ps``) so a book editor or prepress
    operator can audit how each file was produced. When False, only the PDF
    (and optional preview PNG) survive — any prior-run sidecars for the same
    stem are removed on re-export.
    """


@dataclass
class AppConfig:
    """Resolved application config. Use ``load_config()`` to construct."""

    global_color_map: dict[str, dict[str, str]] = field(default_factory=dict)
    cmyk_correction_map: dict[str, dict[str, str]] = field(default_factory=dict)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    print_safety: PrintSafetyConfig = field(default_factory=PrintSafetyConfig)
    png_export: PngExportConfig = field(default_factory=PngExportConfig)
    cmyk_export: CmykExportConfig = field(default_factory=CmykExportConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    log_level: str = "INFO"
    source_path: Optional[Path] = None

    def ensure_dirs(self) -> None:
        """Create the configured input/output/metadata/cmyk directories if missing."""
        for p in (
            self.paths.input_dir,
            self.paths.output_dir,
            self.paths.metadata_dir,
            self.cmyk_export.output_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


def _resolve_path(raw: str, base: Path) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else (base / p).resolve()


def _load_raw(candidates: list[Path], label: str) -> tuple[Optional[Path], dict[str, Any]]:
    for c in candidates:
        if c.is_file():
            log.info("Loading %s from %s", label, c)
            return c, json.loads(c.read_text(encoding="utf-8"))
    log.warning("No %s found; using built-in defaults.", label)
    return None, {}


def load_config() -> AppConfig:
    """
    Load and merge config from two files.

    Paths come from ``config.json`` (fallback: ``config.example.json``).
    Color settings come from ``color-config.json`` (fallback: ``color-config.json.example``).
    """
    path_file, path_raw = _load_raw(
        [PROJECT_ROOT / "config.json", PROJECT_ROOT / "config.example.json"],
        "config.json",
    )
    color_file, color_raw = _load_raw(
        [PROJECT_ROOT / "color-config.json", PROJECT_ROOT / "color-config.json.example"],
        "color-config.json",
    )

    cfg = AppConfig(source_path=path_file or color_file)

    paths = path_raw.get("paths", {})
    base = path_file.parent if path_file else PROJECT_ROOT
    cfg.paths = PathsConfig(
        input_dir=_resolve_path(paths.get("input_dir", "./input"), base),
        output_dir=_resolve_path(paths.get("output_dir", "./output"), base),
        metadata_dir=_resolve_path(paths.get("metadata_dir", "./metadata"), base),
    )

    cfg.global_color_map = {
        k.upper(): v for k, v in color_raw.get("global_color_map", {}).items()
    }
    cfg.cmyk_correction_map = {
        k.upper(): {
            "target": str(v.get("target", "")).upper(),
            "label": str(v.get("label", "")),
            "notes": str(v.get("notes", "")),
        }
        for k, v in color_raw.get("cmyk_correction_map", {}).items()
    }

    matching = color_raw.get("matching", {})
    cfg.matching = MatchingConfig(
        nearest_enabled=bool(matching.get("nearest_enabled", True)),
        metric=str(matching.get("metric", "lab")).lower(),
        threshold=float(matching.get("threshold", 10.0)),
    )

    safety = color_raw.get("print_safety", {})
    cfg.print_safety = PrintSafetyConfig(
        min_gray_value=str(safety.get("min_gray_value", "#EEEEEE")).upper(),
        warn_only=bool(safety.get("warn_only", True)),
    )

    png = path_raw.get("png_export", {})
    cfg.png_export = PngExportConfig(
        enabled=bool(png.get("enabled", True)),
        dpi=int(png.get("dpi", 300)),
        inkscape_path=str(png.get("inkscape_path", "inkscape")),
    )

    cmyk = path_raw.get("cmyk_export", {})
    cfg.cmyk_export = CmykExportConfig(
        enabled=bool(cmyk.get("enabled", True)),
        output_dir=_resolve_path(cmyk.get("output_dir", "./output_cmyk"), base),
        icc_profile_path=_resolve_path(
            cmyk.get("icc_profile_path", "./profiles/ISOcoated_v2_eci.icc"), base
        ),
        ghostscript_path=str(cmyk.get("ghostscript_path", "gswin64c")),
        target_width_inches=float(cmyk.get("target_width_inches", 5.5)),
        target_height_inches=float(cmyk.get("target_height_inches", 7.5)),
        bleed_inches=float(cmyk.get("bleed_inches", 0.0)),
        pdfx_compliance=bool(cmyk.get("pdfx_compliance", False)),
        generate_preview_png=bool(cmyk.get("generate_preview_png", True)),
        preview_dpi=int(cmyk.get("preview_dpi", 150)),
        audit_artifacts=bool(cmyk.get("audit_artifacts", True)),
    )

    cfg.log_level = str(color_raw.get("logging", {}).get("level", "INFO")).upper()
    return cfg


def configure_logging(level: str = "INFO") -> None:
    """Configure root logger once. Idempotent."""
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
