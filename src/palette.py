"""Curated print-color palette.

A *palette* is a small, hand-curated set of source colors that the project
wants to converge on — instead of every illustration carrying its own
slightly-different red, the book settles on (say) three reds that all
illustrations share. Each :class:`Swatch` carries:

  * ``source_hex`` — the sRGB value that gets injected into mappings
    (the value the user picks "from" when they choose a swatch).
  * ``members`` — the original source hexes from the library that should
    converge to this swatch. "Replace globally" rewrites every member in
    every illustration to ``source_hex``.
  * ``label`` / ``notes`` — human metadata, free-form.

The palette is ICC-agnostic in storage: ``source_hex`` is just an sRGB
value, valid under any profile. What *changes* with the profile is the
*displayed appearance* of each swatch (the printed-side roundtrip).
That preview is computed lazily by the caller via
:func:`src.cmyk_gamut.cmyk_roundtrip_rgb` and cached in
``Palette.appearance_cache`` keyed by ``Palette.icc_signature``.

Seeding: :func:`seed_from_hexes` clusters a flat list of source hexes in
CIE Lab using Lloyd's algorithm. The RNG seed is derived from a hash of
the input set so re-running on the same inputs is byte-identical (the
user's "did anything actually change?" sanity check). Different inputs →
different RNG → expected re-cluster.

This module has **no Streamlit dependency**. UI lives in ``app/tab_palette.py``.
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

from .color_mapper import (
    delta_e_lab,
    hex_to_lab,
    hex_to_rgb,
    rgb_to_hex,
)

log = logging.getLogger(__name__)


# Hue families used by :func:`bucketize_for_grid`. Order is the row order
# in the rendered grid. Neutrals always come last (visually settle the eye).
HUE_FAMILIES: tuple[str, ...] = (
    "red", "orange", "yellow", "green", "cyan", "blue", "purple", "neutral"
)

# Below this Lab chroma a color is treated as a neutral, regardless of hue.
# Picked so very-desaturated browns/teals still land in their hue family
# but a true gray collapses to "neutral".
_NEUTRAL_CHROMA_THRESHOLD = 8.0

# How many lightness columns the grid layout produces per hue family.
_DEFAULT_LIGHTNESS_BINS = 6


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass
class Swatch:
    """One palette entry.

    ``id`` is a stable string (``"p_001"`` style) so UI state can reference
    a swatch across reruns even as the list is reordered.
    """

    id: str
    source_hex: str
    label: str = ""
    notes: str = ""
    members: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.source_hex = self.source_hex.upper()
        self.members = sorted({m.upper() for m in self.members})

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "Swatch":
        members_raw = raw.get("members") or []
        if not isinstance(members_raw, list):
            raise ValueError(
                f"Swatch.members must be a list, got {type(members_raw).__name__}"
            )
        return cls(
            id=str(raw.get("id", "")),
            source_hex=str(raw.get("source_hex", "")).upper(),
            label=str(raw.get("label", "")),
            notes=str(raw.get("notes", "")),
            members=[str(m).upper() for m in members_raw],
        )


@dataclass
class Palette:
    """Ordered list of swatches plus an ICC-tagged appearance cache."""

    swatches: list[Swatch] = field(default_factory=list)
    icc_signature: str = ""
    """``"<profile_path>::<mtime>"`` of the ICC the appearance cache was built for."""
    appearance_cache: dict[str, str] = field(default_factory=dict)
    """``{swatch_id: printed_appearance_hex}`` — only valid when ``icc_signature`` matches."""
    version: int = 1

    # ---------- lookup ---------- #
    def find(self, swatch_id: str) -> Optional[Swatch]:
        for s in self.swatches:
            if s.id == swatch_id:
                return s
        return None

    def __iter__(self):
        return iter(self.swatches)

    def __len__(self) -> int:
        return len(self.swatches)

    # ---------- mutation ---------- #
    def next_id(self) -> str:
        """Return a fresh ``p_NNN`` id not already in use."""
        used = {s.id for s in self.swatches}
        for n in range(1, len(used) + 2):
            candidate = f"p_{n:03d}"
            if candidate not in used:
                return candidate
        # Defensive — the loop above always terminates.
        raise RuntimeError("could not allocate swatch id")

    def add(self, source_hex: str, label: str = "", notes: str = "") -> Swatch:
        sw = Swatch(
            id=self.next_id(),
            source_hex=source_hex.upper(),
            label=label,
            notes=notes,
            members=[source_hex.upper()],
        )
        self.swatches.append(sw)
        return sw

    def delete(self, swatch_id: str) -> bool:
        before = len(self.swatches)
        self.swatches = [s for s in self.swatches if s.id != swatch_id]
        self.appearance_cache.pop(swatch_id, None)
        return len(self.swatches) < before

    def merge(self, target_id: str, other_id: str) -> Swatch:
        """Move ``other``'s members into ``target`` and delete ``other``.

        ``target.source_hex`` and metadata are preserved. Raises ``KeyError``
        if either id is unknown, or ``ValueError`` if they're the same.
        """
        if target_id == other_id:
            raise ValueError("cannot merge a swatch into itself")
        target = self.find(target_id)
        other = self.find(other_id)
        if target is None or other is None:
            raise KeyError(f"unknown swatch id(s): {target_id!r} / {other_id!r}")
        merged = sorted(set(target.members) | set(other.members))
        target.members = merged
        self.delete(other_id)
        return target

    def replace_swatches(self, new_swatches: list[Swatch]) -> None:
        """Replace the swatch list (e.g. after re-seeding). Invalidates appearance cache."""
        self.swatches = list(new_swatches)
        self.appearance_cache = {}

    # ---------- appearance cache ---------- #
    def is_appearance_fresh(self, icc_signature: str) -> bool:
        return bool(icc_signature) and self.icc_signature == icc_signature

    def appearance_for(self, swatch_id: str) -> Optional[str]:
        """Return cached printed appearance, or ``None`` if not yet computed."""
        return self.appearance_cache.get(swatch_id)

    # ---------- serialization ---------- #
    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "icc_signature": self.icc_signature,
            "appearance_cache": dict(self.appearance_cache),
            "swatches": [s.to_dict() for s in self.swatches],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "Palette":
        swatches_raw = raw.get("swatches") or []
        if not isinstance(swatches_raw, list):
            raise ValueError(
                f"Palette.swatches must be a list, got {type(swatches_raw).__name__}"
            )
        cache_raw = raw.get("appearance_cache") or {}
        if not isinstance(cache_raw, dict):
            cache_raw = {}
        return cls(
            swatches=[Swatch.from_dict(s) for s in swatches_raw],
            icc_signature=str(raw.get("icc_signature", "")),
            appearance_cache={str(k): str(v).upper() for k, v in cache_raw.items()},
            version=int(raw.get("version", 1)),
        )


# --------------------------------------------------------------------------- #
# Hue / family helpers (used by bucketize_for_grid and tests)
# --------------------------------------------------------------------------- #
def hue_family(hex_color: str) -> str:
    """Classify an sRGB hex into one of :data:`HUE_FAMILIES`.

    Uses Lab chroma + hue angle. Low-chroma colors collapse to ``"neutral"``
    so a near-gray brown doesn't get mis-bucketed as "orange".
    """
    L, a, b = hex_to_lab(hex_color)
    chroma = math.sqrt(a * a + b * b)
    if chroma < _NEUTRAL_CHROMA_THRESHOLD:
        return "neutral"
    # Hue angle in degrees, 0-360. CIE Lab is perceptually based, so the
    # primary sRGB colors don't sit on round angle boundaries — pure red
    # lands near 40°, pure green near 135°, pure blue near 305°. Bins below
    # are tuned to those landing zones; users can manually move borderline
    # swatches in the Palette tab if a cluster lands "wrong".
    angle = math.degrees(math.atan2(b, a)) % 360
    if angle < 50 or angle >= 340:
        return "red"
    if angle < 80:
        return "orange"
    if angle < 115:
        return "yellow"
    if angle < 200:
        return "green"
    if angle < 250:
        return "cyan"
    if angle < 310:
        return "blue"
    return "purple"


def bucketize_for_grid(
    swatches: Iterable[Swatch],
    *,
    lightness_bins: int = _DEFAULT_LIGHTNESS_BINS,
) -> dict[str, list[Optional[Swatch]]]:
    """Lay out swatches as a hue-family-by-lightness grid.

    Returns a dict keyed by hue family (in :data:`HUE_FAMILIES` order) where
    each value is a list of length ``lightness_bins``. Each cell holds either
    the swatch occupying that bin or ``None`` if empty. If two swatches collide
    in the same cell, both are kept by spilling into adjacent free cells in
    the same row — the grid never silently drops a swatch.
    """
    if lightness_bins < 2:
        raise ValueError("lightness_bins must be >= 2")

    grid: dict[str, list[Optional[Swatch]]] = {
        family: [None] * lightness_bins for family in HUE_FAMILIES
    }
    by_family: dict[str, list[tuple[float, Swatch]]] = {f: [] for f in HUE_FAMILIES}
    for sw in swatches:
        family = hue_family(sw.source_hex)
        L, _, _ = hex_to_lab(sw.source_hex)
        by_family[family].append((L, sw))

    # Lab L spans 0..100; map to bin index, then resolve collisions.
    for family, items in by_family.items():
        items.sort(key=lambda it: it[0])
        for L, sw in items:
            preferred = min(
                lightness_bins - 1,
                max(0, int(L / 100.0 * lightness_bins)),
            )
            placed = False
            for offset in range(lightness_bins):
                for delta in (0, -offset, offset) if offset else (0,):
                    idx = preferred + delta
                    if 0 <= idx < lightness_bins and grid[family][idx] is None:
                        grid[family][idx] = sw
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                # Row already full — append a virtual extension so we never drop.
                grid[family].append(sw)
    return grid


# --------------------------------------------------------------------------- #
# Nearest swatch
# --------------------------------------------------------------------------- #
def nearest_swatch(hex_color: str, swatches: Iterable[Swatch]) -> Optional[Swatch]:
    """Return the swatch whose ``source_hex`` is closest to ``hex_color`` in Lab."""
    target_lab = hex_to_lab(hex_color)
    best: Optional[Swatch] = None
    best_dist = math.inf
    for sw in swatches:
        d = delta_e_lab(target_lab, hex_to_lab(sw.source_hex))
        if d < best_dist:
            best_dist = d
            best = sw
    return best


# --------------------------------------------------------------------------- #
# K-means seeding
# --------------------------------------------------------------------------- #
def _deterministic_rng(hexes: list[str]) -> random.Random:
    """Build a Random seeded by a hash of the input hex set.

    Sorting first makes the seed order-insensitive — adding the same
    illustrations in a different order shouldn't reshuffle the palette.
    """
    digest = hashlib.sha256("|".join(sorted(hexes)).encode("ascii")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _lloyd(
    points: list[tuple[float, float, float]],
    k: int,
    rng: random.Random,
    *,
    max_iter: int = 50,
    tol: float = 0.5,
) -> list[int]:
    """Lloyd's k-means on Lab points. Returns a cluster index per point.

    Centroid initialization: random sample without replacement (seeded by
    ``rng``). For our scale (≤ a few hundred points, k ≤ 60) this converges
    reliably; we don't need k-means++.
    """
    n = len(points)
    if k >= n:
        return list(range(n))

    init_idx = rng.sample(range(n), k)
    centroids = [points[i] for i in init_idx]
    assignments = [0] * n

    for _ in range(max_iter):
        # Assign each point to the closest centroid.
        new_assignments = []
        for p in points:
            best_idx = 0
            best_dist = math.inf
            for ci, c in enumerate(centroids):
                d = (p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2 + (p[2] - c[2]) ** 2
                if d < best_dist:
                    best_dist = d
                    best_idx = ci
            new_assignments.append(best_idx)

        # Recompute centroids.
        sums = [[0.0, 0.0, 0.0] for _ in range(k)]
        counts = [0] * k
        for p, a in zip(points, new_assignments):
            sums[a][0] += p[0]
            sums[a][1] += p[1]
            sums[a][2] += p[2]
            counts[a] += 1
        new_centroids: list[tuple[float, float, float]] = []
        for ci in range(k):
            if counts[ci] == 0:
                # Re-seed an empty cluster on a random point — keeps k stable.
                new_centroids.append(points[rng.randrange(n)])
            else:
                new_centroids.append((
                    sums[ci][0] / counts[ci],
                    sums[ci][1] / counts[ci],
                    sums[ci][2] / counts[ci],
                ))

        # Convergence: max centroid drift below tol AND assignments unchanged.
        max_drift = max(
            math.sqrt(
                (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2
            )
            for a, b in zip(centroids, new_centroids)
        )
        centroids = new_centroids
        if max_drift < tol and new_assignments == assignments:
            assignments = new_assignments
            break
        assignments = new_assignments

    return assignments


def seed_from_hexes(hexes: Iterable[str], k: int) -> list[Swatch]:
    """Cluster ``hexes`` in Lab and emit one :class:`Swatch` per cluster.

    For each cluster:
      * ``source_hex`` is the **medoid** — the input hex closest to the
        centroid in Lab. Picking an actual member (rather than the centroid
        rounded to sRGB) keeps the palette inside the original color space
        and avoids fabricating a color that wasn't in any illustration.
      * ``members`` is every input hex assigned to the cluster.
      * ``label`` is a coarse auto-name like ``"warm red"`` derived from
        hue family + lightness, intended as a starting point the user
        edits.

    K-means initialization is seeded deterministically from the input set
    (see :func:`_deterministic_rng`). Same inputs ⇒ same swatches.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    unique = sorted({h.upper() for h in hexes if h})
    if not unique:
        return []

    rng = _deterministic_rng(unique)
    points = [hex_to_lab(h) for h in unique]
    assignments = _lloyd(points, k, rng)

    # Group hexes by cluster.
    clusters: dict[int, list[str]] = {}
    for hex_color, cluster_idx in zip(unique, assignments):
        clusters.setdefault(cluster_idx, []).append(hex_color)

    # Build swatches in a deterministic order (sorted by cluster's mean L
    # then a then b — gives a stable visual order across re-runs).
    cluster_summary: list[tuple[tuple[float, float, float], list[str]]] = []
    for members in clusters.values():
        labs = [hex_to_lab(h) for h in members]
        mean = (
            sum(L for L, _, _ in labs) / len(labs),
            sum(a for _, a, _ in labs) / len(labs),
            sum(b for _, _, b in labs) / len(labs),
        )
        cluster_summary.append((mean, members))
    cluster_summary.sort(key=lambda item: (item[0][0], item[0][1], item[0][2]))

    swatches: list[Swatch] = []
    for idx, (mean, members) in enumerate(cluster_summary, start=1):
        # Medoid: the member whose Lab is closest to the cluster mean.
        medoid = min(members, key=lambda h: delta_e_lab(hex_to_lab(h), mean))
        swatches.append(
            Swatch(
                id=f"p_{idx:03d}",
                source_hex=medoid,
                label=_auto_label(medoid),
                notes="",
                members=sorted(members),
            )
        )
    return swatches


def _auto_label(hex_color: str) -> str:
    """Coarse human-readable label like ``"dark warm red"`` for first-time labelling."""
    L, _, _ = hex_to_lab(hex_color)
    family = hue_family(hex_color)
    if family == "neutral":
        if L > 85:
            return "near white"
        if L < 15:
            return "near black"
        return "neutral gray"
    if L > 75:
        tint = "light"
    elif L < 35:
        tint = "dark"
    else:
        tint = ""
    return f"{tint} {family}".strip()
