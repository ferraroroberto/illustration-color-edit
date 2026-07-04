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
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from .device_cmyk import DeviceCmyk, normalize_device_cmyk_overrides

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


# Fields that flatten a nested JSON group (``trim_to_content``, ``subdirs``)
# onto ``CmykExportConfig`` for convenient attribute access. ``to_json()``
# uses this to reassemble the groups on save the same way ``load_config``
# tears them apart on load.
_CMYK_NESTED_FIELDS: dict[str, tuple[str, str]] = {
    "trim_to_content_enabled": ("trim_to_content", "enabled"),
    "trim_to_content_padding_pt": ("trim_to_content", "padding_pt"),
    "print_subdir": ("subdirs", "print"),
    "preview_subdir": ("subdirs", "preview"),
}


@dataclass
class CmykExportConfig:
    """Configuration for the CMYK print export pipeline.

    Lives under ``cmyk_export`` in ``config.json`` (folder paths) and is the
    sibling of :class:`PngExportConfig` for the grayscale workflow.

    The ICC profile path and Ghostscript binary are user-supplied per machine;
    see ``docs/cmyk-pipeline.md`` for sources.
    """

    enabled: bool = True
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "output_cmyk")
    icc_profile_path: Path = field(default_factory=lambda: PROJECT_ROOT / "profiles" / "ISOcoated_v2_eci.icc")
    ghostscript_path: str = "gswin64c"
    target_width_inches: float = 5.5
    target_height_inches: float = 7.5
    bleed_inches: float = 0.0
    pdfx_compliance: bool | str = False
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

    filename_template: str = ""
    """Output filename template. Empty = ``<stem>_CMYK.pdf`` (historical default).

    Supports ``{stem}``, ``{chapter}``, ``{figure}`` (raw or padded via
    ``{chapter:02d}``), ``{description}``, ``{slug}``. The template
    produces a stem; ``.pdf`` is appended by the pipeline. See
    :mod:`src.filename_template` for the rules.
    """

    tac_limit_percent: float = 320.0
    """Total Area Coverage limit in percent. 320 is a typical coated-stock
    spec; check with the publisher (uncoated is usually 240–280)."""

    tac_check_dpi: int = 100
    """Resolution at which TAC is sampled. 100 is enough for flat-color
    illustrations; raise to 150–200 if features are very fine."""

    force_k_min_stroke_pt: float = 0.5
    """Strokes ≤ this many points (at trim scale) are flagged for force-K."""

    force_k_min_text_pt: float = 9.0
    """Text with font-size ≤ this many points is flagged for force-K."""

    safety_inches: float = 0.1875
    """Safety margin inset from trim. 0.1875" ≈ 4.76 mm — common book default."""

    show_guide_overlay: bool = True
    """Draw trim / bleed / safety rectangles on the soft-proof PNG."""

    trim_to_content_enabled: bool = False
    """Crop the PDF page to the artwork's actual extent (replaces the
    fixed trim). When True, ``target_width_inches``/``target_height_inches``
    and ``bleed_inches`` are bypassed for this file — the page size matches
    the trimmed SVG. Soft-proof guides are suppressed (no margins to draw).
    Default off so existing exports keep their geometry."""

    trim_to_content_padding_pt: float = 0.0
    """Padding (in PostScript points) added around the trimmed bbox on all
    sides. 0 = bbox flush. Range typically 0–20."""

    print_subdir: str = "print"
    """Subfolder under ``output_dir`` that receives the print deliverables
    (PDFs, audit sidecars, the trimmed `_preview_cut.png`, QA report).
    Empty string keeps the historical flat layout."""

    preview_subdir: str = "preview"
    """Subfolder under ``output_dir`` that receives the full uncropped
    client-facing `_preview_full.png` files. Empty string keeps the
    historical flat layout."""

    generate_full_preview: bool = True
    """Also render a second soft-proof PNG at the SVG's natural aspect
    (no trim, no letterbox). Lands in ``output_dir / preview_subdir``
    as `<stem>_CMYK_preview_full.png`. Turn off to skip the second
    Inkscape+Ghostscript pass for that file."""

    render_check: bool = True
    """Diff each SVG's Inkscape render against the RGB PDF render to catch
    Inkscape PDF-export shape-dropping (issue #8) and warn per file. Adds
    one extra Inkscape + Ghostscript render per file; turn off if your
    library is known-clean and you want the fastest batch."""

    render_check_dpi: int = 300
    """Resolution for the render-fidelity diff. 300 dpi resolves a dropped
    emoji-dot-sized shape comfortably above rasteriser anti-alias noise;
    raise only if you suspect even smaller dropped features."""

    @property
    def print_dir(self) -> Path:
        """Resolved directory for print deliverables (PDFs + cut preview)."""
        return self.output_dir / self.print_subdir if self.print_subdir else self.output_dir

    @property
    def preview_dir(self) -> Path:
        """Resolved directory for the full client-facing preview PNGs."""
        return self.output_dir / self.preview_subdir if self.preview_subdir else self.output_dir

    def to_json(self) -> dict[str, Any]:
        """Serialise back to the ``cmyk_export`` JSON shape ``load_config`` reads.

        Walks ``dataclasses.fields()`` so every field on this dataclass is
        included automatically — a new field only needs to be added here
        (implicitly, by existing) rather than also being remembered in a
        hand-rolled save-side dict. ``Path`` fields are normalised to
        strings; the fields that live in nested JSON groups
        (``trim_to_content``, ``subdirs``) are reassembled via
        ``_CMYK_NESTED_FIELDS``, the inverse of how ``load_config`` flattens
        them.
        """
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Path):
                value = str(value)
            nested = _CMYK_NESTED_FIELDS.get(f.name)
            if nested:
                group, key = nested
                out.setdefault(group, {})[key] = value
            else:
                out[f.name] = value
        return out


@dataclass
class AppConfig:
    """Resolved application config. Use ``load_config()`` to construct."""

    global_color_map: dict[str, dict[str, str]] = field(default_factory=dict)
    cmyk_correction_map: dict[str, dict[str, str]] = field(default_factory=dict)
    cmyk_device_overrides: dict[str, DeviceCmyk] = field(default_factory=dict)
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
            self.cmyk_export.print_dir,
            self.cmyk_export.preview_dir,
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


def _build_cmyk_export_config(cmyk: dict[str, Any], base: Path) -> CmykExportConfig:
    """Construct a ``CmykExportConfig`` from the raw ``cmyk_export`` JSON dict.

    Extracted from ``load_config`` so the load-side field-by-field mapping is
    reusable (e.g. by tests exercising the ``to_json`` round trip) without
    going through file I/O.
    """
    return CmykExportConfig(
        enabled=bool(cmyk.get("enabled", True)),
        output_dir=_resolve_path(cmyk.get("output_dir", "./output_cmyk"), base),
        icc_profile_path=_resolve_path(
            cmyk.get("icc_profile_path", "./profiles/ISOcoated_v2_eci.icc"), base
        ),
        ghostscript_path=str(cmyk.get("ghostscript_path", "gswin64c")),
        target_width_inches=float(cmyk.get("target_width_inches", 5.5)),
        target_height_inches=float(cmyk.get("target_height_inches", 7.5)),
        bleed_inches=float(cmyk.get("bleed_inches", 0.0)),
        pdfx_compliance=_coerce_pdfx_compliance(cmyk.get("pdfx_compliance", False)),
        generate_preview_png=bool(cmyk.get("generate_preview_png", True)),
        preview_dpi=int(cmyk.get("preview_dpi", 150)),
        audit_artifacts=bool(cmyk.get("audit_artifacts", True)),
        filename_template=str(cmyk.get("filename_template", "")),
        tac_limit_percent=float(cmyk.get("tac_limit_percent", 320.0)),
        tac_check_dpi=int(cmyk.get("tac_check_dpi", 100)),
        force_k_min_stroke_pt=float(cmyk.get("force_k_min_stroke_pt", 0.5)),
        force_k_min_text_pt=float(cmyk.get("force_k_min_text_pt", 9.0)),
        safety_inches=float(cmyk.get("safety_inches", 0.1875)),
        show_guide_overlay=bool(cmyk.get("show_guide_overlay", True)),
        trim_to_content_enabled=bool(
            cmyk.get("trim_to_content", {}).get("enabled", False)
        ),
        trim_to_content_padding_pt=float(
            cmyk.get("trim_to_content", {}).get("padding_pt", 0.0)
        ),
        print_subdir=str(cmyk.get("subdirs", {}).get("print", "print")),
        preview_subdir=str(cmyk.get("subdirs", {}).get("preview", "preview")),
        generate_full_preview=bool(cmyk.get("generate_full_preview", True)),
        render_check=bool(cmyk.get("render_check", True)),
        render_check_dpi=int(cmyk.get("render_check_dpi", 300)),
    )


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
    cfg.cmyk_device_overrides = normalize_device_cmyk_overrides(
        color_raw.get("cmyk_device_overrides", {})
    )

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
    cfg.cmyk_export = _build_cmyk_export_config(cmyk, base)

    cfg.log_level = str(color_raw.get("logging", {}).get("level", "INFO")).upper()
    return cfg


def _coerce_pdfx_compliance(value: object) -> bool | str:
    """Keep legacy bool config while accepting explicit PDF/X variants."""
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text:
        return False
    low = text.lower()
    if low in {"false", "off", "none", "no", "0"}:
        return False
    if low in {"true", "on", "yes", "1", "pdf/x-1a", "pdf/x-1a:2003", "x1a"}:
        return "PDF/X-1a:2003"
    if low in {"pdf/x-4", "pdf/x4", "x4", "pdfx-4"}:
        return "PDF/X-4"
    return text


def configure_logging(level: str = "INFO") -> None:
    """Configure root logger once. Idempotent."""
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
