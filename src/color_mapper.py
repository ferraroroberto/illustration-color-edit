"""
Color mapping engine.

Given a source color extracted from an SVG, decide what target color it
should be remapped to:

  1. **Exact match** — the source hex is a key in the global color map.
  2. **Nearest match** — within a configurable distance threshold (CIE
     Lab ΔE or RGB Euclidean) of an existing key.
  3. **No match** — must be assigned manually.

The mapper is pure: it knows nothing about files. The caller passes in a
``global_map`` (dict keyed by canonical ``#RRGGBB`` uppercase) and a
:class:`MatchingConfig` (from ``src.config``).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

from .config import MatchingConfig

log = logging.getLogger(__name__)


class MatchKind(str, Enum):
    EXACT = "exact"
    NEAR = "near"
    NONE = "none"


@dataclass(frozen=True)
class Suggestion:
    """A proposed mapping for one source color."""

    source: str                       # canonical source hex (#RRGGBB upper)
    target: Optional[str]             # canonical target hex, or None if NONE
    kind: MatchKind
    distance: float = 0.0
    via: Optional[str] = None         # the global-map key that matched (for NEAR)
    label: Optional[str] = None
    notes: Optional[str] = None

    @property
    def is_actionable(self) -> bool:
        """True if this suggestion can be applied without user input."""
        return self.kind is not MatchKind.NONE and self.target is not None


# --------------------------------------------------------------------------- #
# Color space conversions and distance metrics
# --------------------------------------------------------------------------- #
def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """``#RRGGBB`` -> ``(r, g, b)`` in 0..255."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"Expected 6-digit hex, got {hex_color!r}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _srgb_to_linear(c: float) -> float:
    """Inverse sRGB gamma. ``c`` is in 0..1."""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_xyz(r: int, g: int, b: int) -> tuple[float, float, float]:
    """sRGB (0..255) -> CIE XYZ (D65, scaled 0..100)."""
    rl = _srgb_to_linear(r / 255.0)
    gl = _srgb_to_linear(g / 255.0)
    bl = _srgb_to_linear(b / 255.0)
    # sRGB D65 matrix
    x = (rl * 0.4124564 + gl * 0.3575761 + bl * 0.1804375) * 100
    y = (rl * 0.2126729 + gl * 0.7151522 + bl * 0.0721750) * 100
    z = (rl * 0.0193339 + gl * 0.1191920 + bl * 0.9503041) * 100
    return x, y, z


# D65 reference white, scaled 0..100
_REF_X, _REF_Y, _REF_Z = 95.047, 100.000, 108.883


def _f_lab(t: float) -> float:
    delta = 6 / 29
    if t > delta**3:
        return t ** (1 / 3)
    return t / (3 * delta**2) + 4 / 29


def xyz_to_lab(x: float, y: float, z: float) -> tuple[float, float, float]:
    """CIE XYZ -> CIE L*a*b*."""
    fx = _f_lab(x / _REF_X)
    fy = _f_lab(y / _REF_Y)
    fz = _f_lab(z / _REF_Z)
    L = 116 * fy - 16
    a = 500 * (fx - fy)
    b = 200 * (fy - fz)
    return L, a, b


def hex_to_lab(hex_color: str) -> tuple[float, float, float]:
    return xyz_to_lab(*rgb_to_xyz(*hex_to_rgb(hex_color)))


def delta_e_lab(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """ΔE76 (Euclidean distance in CIE L*a*b*)."""
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def rgb_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Euclidean distance in sRGB."""
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def color_distance(hex_a: str, hex_b: str, metric: str = "lab") -> float:
    """
    Distance between two ``#RRGGBB`` colors.

    ``metric`` is ``"lab"`` (CIE ΔE76) or ``"rgb"`` (Euclidean in 0..255).
    """
    metric = metric.lower()
    if metric == "lab":
        return delta_e_lab(hex_to_lab(hex_a), hex_to_lab(hex_b))
    if metric == "rgb":
        return rgb_distance(hex_to_rgb(hex_a), hex_to_rgb(hex_b))
    raise ValueError(f"Unknown metric: {metric!r}. Use 'lab' or 'rgb'.")


def is_grayscale(hex_color: str, tolerance: int = 2) -> bool:
    """True if R, G, B channels are within ``tolerance`` of each other."""
    r, g, b = hex_to_rgb(hex_color)
    return max(r, g, b) - min(r, g, b) <= tolerance


def gray_value(hex_color: str) -> int:
    """Perceptual luminance 0..255 (Rec. 709)."""
    r, g, b = hex_to_rgb(hex_color)
    return round(0.2126 * r + 0.7152 * g + 0.0722 * b)


# --------------------------------------------------------------------------- #
# Mapper
# --------------------------------------------------------------------------- #
@dataclass
class ColorMapper:
    """
    Suggest target colors for source colors using a global color map plus
    optional nearest-color matching.

    The global map is the dict from ``config.json``:

        {"#E74C3C": {"target": "#333333", "label": "...", "notes": "..."}}

    Per-illustration overrides are layered on top via :meth:`with_overrides`.
    """

    global_map: dict[str, dict[str, str]] = field(default_factory=dict)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    overrides: dict[str, str] = field(default_factory=dict)
    _lab_cache: dict[str, tuple[float, float, float]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        # Normalize keys to canonical form.
        self.global_map = {k.upper(): v for k, v in self.global_map.items()}
        self.overrides = {k.upper(): v.upper() for k, v in self.overrides.items()}

    # ----- caching helpers ------------------------------------------------- #
    def _lab(self, hex_color: str) -> tuple[float, float, float]:
        cached = self._lab_cache.get(hex_color)
        if cached is None:
            cached = hex_to_lab(hex_color)
            self._lab_cache[hex_color] = cached
        return cached

    def _distance(self, a: str, b: str) -> float:
        if self.matching.metric == "lab":
            return delta_e_lab(self._lab(a), self._lab(b))
        return rgb_distance(hex_to_rgb(a), hex_to_rgb(b))

    # ----- public API ------------------------------------------------------ #
    def with_overrides(self, overrides: dict[str, str]) -> "ColorMapper":
        """Return a shallow copy with per-illustration overrides applied."""
        merged = dict(self.overrides)
        merged.update({k.upper(): v.upper() for k, v in overrides.items()})
        return ColorMapper(
            global_map=self.global_map,
            matching=self.matching,
            overrides=merged,
        )

    def suggest(self, source_hex: str) -> Suggestion:
        """Suggest a target for ``source_hex``."""
        src = source_hex.upper()

        # Per-illustration override always wins.
        if src in self.overrides:
            return Suggestion(
                source=src,
                target=self.overrides[src],
                kind=MatchKind.EXACT,
                distance=0.0,
                via=src,
                label="override",
            )

        # Exact match against the global map.
        if src in self.global_map:
            entry = self.global_map[src]
            return Suggestion(
                source=src,
                target=entry["target"].upper(),
                kind=MatchKind.EXACT,
                distance=0.0,
                via=src,
                label=entry.get("label"),
                notes=entry.get("notes"),
            )

        # Optional nearest match.
        if self.matching.nearest_enabled and self.global_map:
            best_key, best_dist = self._closest(src)
            if best_key is not None and best_dist <= self.matching.threshold:
                entry = self.global_map[best_key]
                return Suggestion(
                    source=src,
                    target=entry["target"].upper(),
                    kind=MatchKind.NEAR,
                    distance=best_dist,
                    via=best_key,
                    label=entry.get("label"),
                    notes=entry.get("notes"),
                )

        return Suggestion(source=src, target=None, kind=MatchKind.NONE)

    def suggest_many(self, sources: Iterable[str]) -> list[Suggestion]:
        return [self.suggest(s) for s in sources]

    def resolve(
        self,
        source_hex: str,
        manual: Optional[str] = None,
    ) -> Optional[str]:
        """
        Return the concrete target hex for ``source_hex``.

        Resolution order:
          1. ``manual`` if provided (validated to ``#RRGGBB``)
          2. :meth:`suggest` if it returns an actionable suggestion
          3. ``None`` (caller must handle unmapped)
        """
        if manual:
            m = manual.upper()
            if not (m.startswith("#") and len(m) == 7):
                raise ValueError(f"Manual override must be #RRGGBB, got {manual!r}")
            return m
        s = self.suggest(source_hex)
        return s.target

    def apply_to_palette(
        self,
        palette: Iterable[str],
        manual: Optional[dict[str, str]] = None,
    ) -> dict[str, Optional[str]]:
        """
        Resolve every color in ``palette`` to a target.

        ``manual`` is an optional dict of explicit user picks
        (source_hex -> target_hex) that override suggestions.
        """
        manual_norm = {k.upper(): v.upper() for k, v in (manual or {}).items()}
        return {
            src.upper(): self.resolve(src, manual=manual_norm.get(src.upper()))
            for src in palette
        }

    # ----- internals ------------------------------------------------------- #
    def _closest(self, src: str) -> tuple[Optional[str], float]:
        best_key: Optional[str] = None
        best_dist = math.inf
        for key in self.global_map:
            d = self._distance(src, key)
            if d < best_dist:
                best_dist = d
                best_key = key
        return best_key, best_dist


# --------------------------------------------------------------------------- #
# Cross-library suggestions
# --------------------------------------------------------------------------- #
def suggest_from_history(
    source_hex: str,
    history: dict[str, dict[str, int]],
) -> list[tuple[str, int]]:
    """
    Given the cross-library history of how ``source_hex`` has been mapped
    in other illustrations, return ``[(target_hex, count), ...]`` sorted by
    most-used first.

    ``history`` shape: ``{source_hex: {target_hex: count}}``.
    """
    src = source_hex.upper()
    by_target = history.get(src, {})
    return sorted(
        ((t.upper(), c) for t, c in by_target.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
