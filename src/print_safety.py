"""
Print-safety checks for grayscale targets.

Output is destined for an uncoated book paper print run. Very light grays
(say lighter than ``#EEEEEE``) tend to disappear or look like the paper
itself. This module flags any target gray that's lighter than the
configured threshold so the user can pick a darker value.

Threshold is configurable in ``config.json`` under ``print_safety``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from .color_mapper import gray_value, hex_to_rgb, is_grayscale
from .config import PrintSafetyConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SafetyWarning:
    """A single print-safety concern about one target color."""

    target: str            # canonical #RRGGBB
    sources: tuple[str, ...]   # source hexes that map to this target
    reason: str
    luminance: int         # 0..255

    def __str__(self) -> str:
        srcs = ", ".join(self.sources) if self.sources else "(unused)"
        return f"{self.target} (luminance={self.luminance}) — {self.reason} (used by: {srcs})"


def _luminance_threshold(threshold_hex: str) -> int:
    """Convert ``#EEEEEE`` -> integer luminance for fast comparison."""
    return gray_value(threshold_hex)


def check_target(
    target_hex: str,
    config: PrintSafetyConfig,
    sources: Optional[Iterable[str]] = None,
) -> Optional[SafetyWarning]:
    """
    Return a :class:`SafetyWarning` if ``target_hex`` is unsafe for print,
    else ``None``.

    Currently flags two conditions:

      * Luminance lighter than ``config.min_gray_value``.
      * Target is not actually grayscale (R/G/B differ noticeably) — this
        usually means the user picked a tinted color by mistake.
    """
    target = target_hex.upper()
    lum = gray_value(target)
    threshold = _luminance_threshold(config.min_gray_value)
    src_tuple = tuple(s.upper() for s in (sources or ()))

    if lum > threshold:
        return SafetyWarning(
            target=target,
            sources=src_tuple,
            reason=f"luminance {lum} is lighter than configured min {threshold} ({config.min_gray_value})",
            luminance=lum,
        )

    if not is_grayscale(target, tolerance=4):
        r, g, b = hex_to_rgb(target)
        return SafetyWarning(
            target=target,
            sources=src_tuple,
            reason=f"target is not grayscale (R={r}, G={g}, B={b}) — channels should match",
            luminance=lum,
        )

    return None


def check_mapping(
    mapping: dict[str, str],
    config: PrintSafetyConfig,
) -> list[SafetyWarning]:
    """
    Run :func:`check_target` over every target in ``mapping``.

    ``mapping`` is the flat ``source_hex -> target_hex`` form (i.e. the
    output of :func:`mapping_store.merge_mappings`).
    """
    by_target: dict[str, list[str]] = {}
    for src, tgt in mapping.items():
        by_target.setdefault(tgt.upper(), []).append(src.upper())

    warnings: list[SafetyWarning] = []
    for target, sources in by_target.items():
        w = check_target(target, config, sources=sources)
        if w is not None:
            warnings.append(w)
    return warnings
