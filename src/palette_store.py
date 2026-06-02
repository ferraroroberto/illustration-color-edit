"""Persistence for the curated palette (``palette.json``).

The palette lives next to ``color-config.json`` (one shared file per project,
gitignored). Storage uses the same atomic tempfile-+-rename pattern as
:mod:`src.mapping_store` so a crashed write can never leave a half-written
file in place.

Profile-dependent fields (``icc_signature`` + ``appearance_cache``) are
written through verbatim — invalidation is the caller's responsibility:
when the active ICC changes, the caller compares the current
:func:`make_icc_signature` against the stored one and rebuilds the cache.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .mapping_store import _atomic_write_json
from .palette import Palette

log = logging.getLogger(__name__)


def make_icc_signature(icc_path: Path) -> str:
    """Build a cache key for an ICC profile from its absolute path + mtime.

    Returns ``""`` if the profile cannot be stat'd — callers should treat
    that as "no signature available, regenerate appearance from scratch".
    """
    try:
        p = Path(icc_path).resolve()
        return f"{p}::{p.stat().st_mtime}"
    except (OSError, ValueError):
        return ""


@dataclass
class PaletteStore:
    """Load / save :class:`Palette` to ``palette_path``."""

    palette_path: Path

    def __post_init__(self) -> None:
        self.palette_path = Path(self.palette_path)

    def load(self) -> Palette:
        """Return the persisted palette, or an empty one if no file exists yet."""
        if not self.palette_path.is_file():
            return Palette()
        try:
            raw = json.loads(self.palette_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log.error(
                "Corrupt palette at %s: %s. Returning empty palette.",
                self.palette_path,
                exc,
            )
            return Palette()
        return Palette.from_dict(raw)

    def save(self, palette: Palette) -> None:
        """Persist ``palette`` atomically."""
        _atomic_write_json(self.palette_path, palette.to_dict())

    def delete(self) -> bool:
        """Remove the palette file, if it exists. Returns whether anything was removed."""
        if self.palette_path.is_file():
            self.palette_path.unlink()
            return True
        return False
