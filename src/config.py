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
class PathsConfig:
    input_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "input")
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "output")
    metadata_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "metadata")


@dataclass
class AppConfig:
    """Resolved application config. Use ``load_config()`` to construct."""

    global_color_map: dict[str, dict[str, str]] = field(default_factory=dict)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    print_safety: PrintSafetyConfig = field(default_factory=PrintSafetyConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    log_level: str = "INFO"
    source_path: Optional[Path] = None

    def ensure_dirs(self) -> None:
        """Create the configured input/output/metadata directories if missing."""
        for p in (self.paths.input_dir, self.paths.output_dir, self.paths.metadata_dir):
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

    cfg.log_level = str(color_raw.get("logging", {}).get("level", "INFO")).upper()
    return cfg


def configure_logging(level: str = "INFO") -> None:
    """Configure root logger once. Idempotent."""
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
