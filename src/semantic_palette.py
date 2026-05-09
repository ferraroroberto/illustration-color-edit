"""Semantic palette: named slots layered on top of hex mappings.

Two-line elevator pitch: today's pipeline maps ``#E74C3C → #373737``
straight at the hex level. With this module, you bind ``#E74C3C`` to a
*slot* (``status.bad``) and the slot to a per-pipeline target inside an
active *theme*. Now the global map only knows the slot's identity, and
the per-pipeline targets live in one place — the theme. Re-skinning for
a different publisher = swap themes, no per-illustration touching.

Resolution order at apply time (both pipelines):

  1. Per-file ``overrides`` / ``cmyk_overrides`` (highest priority).
  2. Active theme: source hex → owning slot → ``theme.{cmyk|grayscale}[slot]``.
  3. Existing global map (``global_color_map`` / ``cmyk_correction_map``).
  4. Pass-through (no remap).

The semantic layer is **additive**: existing global-map entries still
work for any color a slot doesn't claim, so this can be adopted
incrementally without a forced migration.

Persistence: ``semantic-palette.json`` at the project root, gitignored,
with a ``.example`` template committed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from .mapping_store import _atomic_write_json

log = logging.getLogger(__name__)

Pipeline = Literal["cmyk", "grayscale"]


@dataclass
class Slot:
    """One named slot. ``authored`` is the source hex an artist actually used."""

    name: str
    authored: str  # canonical "#RRGGBB"
    label: str = ""
    notes: str = ""


@dataclass
class Theme:
    """Per-pipeline target hexes for each slot."""

    cmyk: dict[str, str] = field(default_factory=dict)       # slot_name → hex
    grayscale: dict[str, str] = field(default_factory=dict)  # slot_name → hex


@dataclass
class SemanticPalette:
    """In-memory palette of slots + themes."""

    slots: dict[str, Slot] = field(default_factory=dict)
    themes: dict[str, Theme] = field(default_factory=dict)
    active_theme: str = "default"

    # ----- queries -------------------------------------------------------- #
    def slot_for_hex(self, source_hex: str) -> Optional[Slot]:
        """Return the slot whose ``authored`` equals ``source_hex``, or None."""
        target = source_hex.upper()
        for slot in self.slots.values():
            if slot.authored.upper() == target:
                return slot
        return None

    def active(self) -> Theme:
        """Return the active theme (creates an empty one if missing)."""
        return self.themes.setdefault(self.active_theme, Theme())

    def resolve(self, source_hex: str, pipeline: Pipeline) -> Optional[str]:
        """Return the slot-driven target for ``source_hex`` or None.

        ``None`` means "no slot claims this color OR the active theme has
        no entry for that slot in that pipeline" — caller falls back to
        the legacy global map.
        """
        slot = self.slot_for_hex(source_hex)
        if slot is None:
            return None
        theme = self.themes.get(self.active_theme)
        if theme is None:
            return None
        target = (
            theme.cmyk.get(slot.name) if pipeline == "cmyk"
            else theme.grayscale.get(slot.name)
        )
        if not target:
            return None
        return target.upper()

    def theme_overrides(self, pipeline: Pipeline) -> dict[str, str]:
        """Flatten the active theme into a ``{source_hex: target_hex}`` dict.

        Used by the merge layer to inject the theme's hex picks before
        the legacy global map. Slots without a target in the active
        theme don't appear (the legacy map keeps governing them).
        """
        out: dict[str, str] = {}
        theme = self.themes.get(self.active_theme)
        if theme is None:
            return out
        targets = theme.cmyk if pipeline == "cmyk" else theme.grayscale
        for slot_name, slot in self.slots.items():
            if slot_name in targets and targets[slot_name]:
                out[slot.authored.upper()] = targets[slot_name].upper()
        return out

    # ----- mutations ------------------------------------------------------ #
    def upsert_slot(
        self,
        name: str,
        authored: str,
        label: str = "",
        notes: str = "",
    ) -> Slot:
        slot = Slot(
            name=name,
            authored=authored.upper(),
            label=label,
            notes=notes,
        )
        self.slots[name] = slot
        return slot

    def remove_slot(self, name: str) -> bool:
        if name not in self.slots:
            return False
        del self.slots[name]
        # Also strip from every theme so we don't accumulate orphans.
        for theme in self.themes.values():
            theme.cmyk.pop(name, None)
            theme.grayscale.pop(name, None)
        return True

    def set_theme_target(
        self,
        slot_name: str,
        pipeline: Pipeline,
        target_hex: str,
    ) -> None:
        if slot_name not in self.slots:
            raise KeyError(f"Unknown slot {slot_name!r}")
        theme = self.themes.setdefault(self.active_theme, Theme())
        target = target_hex.upper()
        if pipeline == "cmyk":
            theme.cmyk[slot_name] = target
        else:
            theme.grayscale[slot_name] = target

    def clear_theme_target(self, slot_name: str, pipeline: Pipeline) -> bool:
        theme = self.themes.get(self.active_theme)
        if theme is None:
            return False
        bucket = theme.cmyk if pipeline == "cmyk" else theme.grayscale
        if slot_name in bucket:
            del bucket[slot_name]
            return True
        return False

    # ----- serialization -------------------------------------------------- #
    def to_dict(self) -> dict[str, object]:
        return {
            "active_theme": self.active_theme,
            "slots": {
                name: {
                    "authored": s.authored.upper(),
                    "label": s.label,
                    "notes": s.notes,
                }
                for name, s in self.slots.items()
            },
            "themes": {
                tname: {
                    "cmyk": {k: v.upper() for k, v in t.cmyk.items()},
                    "grayscale": {k: v.upper() for k, v in t.grayscale.items()},
                }
                for tname, t in self.themes.items()
            },
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SemanticPalette":
        slots_raw = raw.get("slots", {}) or {}
        slots: dict[str, Slot] = {}
        for name, entry in slots_raw.items():
            if not isinstance(entry, dict):
                continue
            slots[name] = Slot(
                name=name,
                authored=str(entry.get("authored", "")).upper(),
                label=str(entry.get("label", "")),
                notes=str(entry.get("notes", "")),
            )

        themes_raw = raw.get("themes", {}) or {}
        themes: dict[str, Theme] = {}
        for tname, tentry in themes_raw.items():
            if not isinstance(tentry, dict):
                continue
            themes[tname] = Theme(
                cmyk={
                    str(k): str(v).upper()
                    for k, v in (tentry.get("cmyk") or {}).items()
                },
                grayscale={
                    str(k): str(v).upper()
                    for k, v in (tentry.get("grayscale") or {}).items()
                },
            )

        return cls(
            slots=slots,
            themes=themes,
            active_theme=str(raw.get("active_theme", "default")),
        )


# --------------------------------------------------------------------------- #
# Disk persistence
# --------------------------------------------------------------------------- #
@dataclass
class SemanticPaletteStore:
    """Atomic JSON read/write for ``semantic-palette.json``."""

    path: Path

    def load(self) -> SemanticPalette:
        if not self.path.is_file():
            return SemanticPalette()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log.error("Corrupt %s: %s. Returning empty palette.", self.path, exc)
            return SemanticPalette()
        return SemanticPalette.from_dict(raw)

    def save(self, palette: SemanticPalette) -> None:
        _atomic_write_json(self.path, palette.to_dict())


# --------------------------------------------------------------------------- #
# Merge integration
# --------------------------------------------------------------------------- #
def merge_with_semantic(
    global_map: dict[str, dict[str, str]],
    overrides: dict[str, str],
    semantic: Optional[SemanticPalette],
    pipeline: Pipeline,
) -> dict[str, str]:
    """Flatten layers into the final ``{source_hex: target_hex}`` writer dict.

    Order applied (later writes win):
      1. Existing global map (legacy fallback).
      2. Active theme (semantic palette) — only the slots with a target
         in the active theme contribute.
      3. Per-illustration overrides (highest priority).

    When ``semantic`` is ``None`` or empty, this degenerates to the
    legacy ``mapping_store.merge_mappings`` behavior.
    """
    out: dict[str, str] = {
        k.upper(): v["target"].upper()
        for k, v in global_map.items()
        if v.get("target")
    }
    if semantic is not None:
        out.update(semantic.theme_overrides(pipeline))
    out.update({k.upper(): v.upper() for k, v in overrides.items()})
    return out


def auto_migrate_global_map(
    semantic: SemanticPalette,
    global_map: dict[str, dict[str, str]],
    pipeline: Pipeline,
    *,
    name_prefix: str = "auto",
) -> int:
    """Promote unbound global-map entries to auto-named slots.

    Walks ``global_map`` and for each ``source_hex`` that isn't already
    claimed by an existing slot, creates a slot ``"{name_prefix}.NNN"``
    with ``authored=source_hex`` and ``label=entry.label``, and wires
    its theme target to ``entry.target``. Idempotent: rerunning is safe
    — already-claimed sources are skipped.

    Returns the number of slots created. The caller decides whether to
    persist the palette afterwards.
    """
    existing_authored = {s.authored.upper() for s in semantic.slots.values()}
    next_n = 1
    used_names = set(semantic.slots.keys())
    while f"{name_prefix}.{next_n:03d}" in used_names:
        next_n += 1

    created = 0
    for src, entry in global_map.items():
        src_u = src.upper()
        target = entry.get("target", "").upper()
        if not target or src_u in existing_authored:
            continue
        name = f"{name_prefix}.{next_n:03d}"
        while name in used_names:
            next_n += 1
            name = f"{name_prefix}.{next_n:03d}"
        used_names.add(name)
        existing_authored.add(src_u)
        semantic.upsert_slot(
            name=name,
            authored=src_u,
            label=entry.get("label", "") or src_u,
            notes=entry.get("notes", ""),
        )
        semantic.set_theme_target(name, pipeline, target)
        created += 1
        next_n += 1
    return created


__all__ = [
    "Slot",
    "Theme",
    "SemanticPalette",
    "SemanticPaletteStore",
    "merge_with_semantic",
    "auto_migrate_global_map",
]
