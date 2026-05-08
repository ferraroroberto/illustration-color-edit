"""CMYK gamut check via real ICC roundtrip.

Given a target sRGB color and a CMYK ICC profile, compute the perceptual
shift (Lab ΔE76) the color will undergo when printed through that profile
on press: ``sRGB → CMYK (ICC) → sRGB → ΔE``.

A large value means the color sits outside (or near the edge of) the press's
gamut — saturated reds, vivid greens, and pure cyans typically clip in
SWOP/GRACoL profiles. The CMYK Editor uses this to warn the user before
they commit a color that won't reproduce faithfully.

Why a roundtrip rather than a direct gamut-volume test? Because what the
user actually sees on press is the round-trip result: the same ICC engine
that converts forward also defines what the proof shows back. Comparing
``original`` to ``round-tripped`` is the most honest "how off will this
look" answer, and it's what production proofing software does.

Implementation notes:
  * ``PIL.ImageCms`` (lcms2 under the hood) is used for the ICC math —
    no Ghostscript invocation per color.
  * Transforms are cached per ``(profile-path, profile-mtime)`` via
    :func:`functools.lru_cache` so repeated lookups in a Streamlit rerun
    are essentially free after the first call.
  * ΔE76 is the simple Lab Euclidean metric. Newer formulas (ΔE2000) are
    more perceptually uniform but the difference doesn't change which
    colors get flagged at our threshold (~6).
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageCms


# PIL ships an sRGB profile builder; one instance is enough.
_SRGB_PROFILE = ImageCms.createProfile("sRGB")


def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _srgb_to_lab(r: int, g: int, b: int) -> Tuple[float, float, float]:
    """sRGB (0-255) → Lab via D65 reference white (CIE 1976)."""
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    rl, gl, bl = lin(r), lin(g), lin(b)
    # sRGB → XYZ.
    X = rl * 0.4124564 + gl * 0.3575761 + bl * 0.1804375
    Y = rl * 0.2126729 + gl * 0.7151522 + bl * 0.0721750
    Z = rl * 0.0193339 + gl * 0.1191920 + bl * 0.9503041
    # Normalize by D65 reference white.
    X /= 0.95047
    Y /= 1.00000
    Z /= 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)

    fx, fy, fz = f(X), f(Y), f(Z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e_76(rgb1: Tuple[int, int, int], rgb2: Tuple[int, int, int]) -> float:
    """ΔE76 between two sRGB colors. Pure helper, no I/O."""
    L1, a1, b1 = _srgb_to_lab(*rgb1)
    L2, a2, b2 = _srgb_to_lab(*rgb2)
    return math.sqrt((L1 - L2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2)


@lru_cache(maxsize=8)
def _transforms_for(icc_path_str: str, icc_mtime: float):
    """Build cached ImageCms transforms for a given ICC profile.

    ``icc_mtime`` is part of the cache key so swapping the file at runtime
    invalidates the cached transforms automatically.
    """
    icc = ImageCms.getOpenProfile(icc_path_str)
    # Default rendering intent is INTENT_PERCEPTUAL (0), which matches what
    # Ghostscript uses by default and is what most prepress workflows expect.
    forward = ImageCms.buildTransform(_SRGB_PROFILE, icc, "RGB", "CMYK")
    backward = ImageCms.buildTransform(icc, _SRGB_PROFILE, "CMYK", "RGB")
    return forward, backward


def cmyk_gamut_delta(hex_color: str, icc_path: Path) -> Optional[float]:
    """Return Lab ΔE76 of ``hex_color`` round-tripped through the ICC profile.

    Returns ``None`` if the profile cannot be loaded — the caller should
    treat that as "no warning available" rather than "no warning needed".

    A larger value means the color shifts more when printed on press.
    Useful thresholds:

      * < 1   : not perceptible — color is well inside gamut
      * 1-2   : barely perceptible
      * 2-6   : perceptible at a glance, but acceptable for most work
      * > 6   : noticeably different — flag the user
      * > 10  : strongly out of gamut
    """
    icc_path = Path(icc_path)
    if not icc_path.is_file():
        return None
    try:
        forward, backward = _transforms_for(
            str(icc_path), icc_path.stat().st_mtime
        )
    except (OSError, ImageCms.PyCMSError):
        return None

    rgb_in = _hex_to_rgb(hex_color)
    src_img = Image.new("RGB", (1, 1), rgb_in)
    cmyk_img = ImageCms.applyTransform(src_img, forward)
    rgb_back_img = ImageCms.applyTransform(cmyk_img, backward)
    rgb_out = rgb_back_img.getpixel((0, 0))
    return delta_e_76(rgb_in, rgb_out)
