"""Color-blindness simulation and risk detection.

Implements Machado-Oliveira-Fernandes (2009) sRGB matrices for the three
common cone deficiencies, plus BT.709 luma for achromatopsia. Acts on
hex colors so the same simulation can be applied at the SVG-level
(remap each color into its CB-perceived equivalent and re-render the
illustration) without any rasterization.

Risk detection compares the *distinguishability* of color pairs before
and after simulation. If two colors that were clearly distinct in the
original (ΔE76 > 25) collapse to nearly the same color (< 10) under a
simulation, the illustration is flagged for that CB type — the
semantic ordering carried by color is no longer perceivable for that
audience.

Reference: Machado, Oliveira, Fernandes. "A Physiologically-based Model
for Simulation of Color Vision Deficiency." IEEE Transactions on
Visualization and Computer Graphics, 2009.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

import numpy as np

from .cmyk_gamut import _hex_to_rgb, delta_e_76

CbType = Literal["normal", "deutan", "protan", "tritan", "achromat"]
CB_TYPES: tuple[CbType, ...] = ("deutan", "protan", "tritan", "achromat")


# Approximate population frequencies (men / women).
POPULATION_PCT: dict[CbType, str] = {
    "deutan":   "~6% male / 0.4% female",
    "protan":   "~2% male / 0.01% female",
    "tritan":   "~0.01% (rare)",
    "achromat": "~0.003% (very rare)",
}

# Machado (2009) sRGB matrices at severity = 1.0. Source:
#   https://www.inf.ufrgs.br/~oliveira/pubs_files/CVD_Simulation/CVD_Simulation.html
# Applied to *linear* RGB; we convert sRGB → linear → apply → linear → sRGB.
_PROTAN_1 = np.array([
    [0.152286, 1.052583, -0.204868],
    [0.114503, 0.786281,  0.099216],
    [-0.003882, -0.048116, 1.051998],
])

_DEUTAN_1 = np.array([
    [0.367322,  0.860646, -0.227968],
    [0.280085,  0.672501,  0.047413],
    [-0.011820,  0.042940,  0.968881],
])

_TRITAN_1 = np.array([
    [1.255528, -0.076749, -0.178779],
    [-0.078411, 0.930809,  0.147602],
    [0.004733,  0.691367,  0.303900],
])

_MATRICES: dict[str, np.ndarray] = {
    "protan": _PROTAN_1,
    "deutan": _DEUTAN_1,
    "tritan": _TRITAN_1,
}


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    a = c / 255.0
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c: np.ndarray) -> np.ndarray:
    out = np.where(c <= 0.0031308, 12.92 * c, 1.055 * (c ** (1.0 / 2.4)) - 0.055)
    return np.clip(out * 255.0, 0, 255)


def _interpolate_matrix(name: str, severity: float) -> np.ndarray:
    """Linearly interpolate from identity at severity=0 to the full matrix at 1."""
    s = max(0.0, min(1.0, severity))
    M = _MATRICES[name]
    return (1.0 - s) * np.eye(3) + s * M


def simulate_hex(hex_color: str, cb_type: CbType, severity: float = 1.0) -> str:
    """Return the simulated sRGB hex for ``hex_color`` under ``cb_type``.

    ``severity`` is a continuous slider from 0.0 (no deficiency, identity)
    to 1.0 (full deficiency). The Machado matrices are defined at
    severity 1.0; intermediate values are linearly interpolated against
    the identity matrix — the same approach Machado et al. recommend.

    For ``"achromat"`` the input is converted to BT.709 luma; severity
    is honored in the same way (mix of original and luma).
    """
    if cb_type == "normal":
        return hex_color.upper()

    rgb = np.array(_hex_to_rgb(hex_color), dtype=np.float32)

    if cb_type == "achromat":
        lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
        s = max(0.0, min(1.0, severity))
        out = (1.0 - s) * rgb + s * np.array([lum, lum, lum])
        out = np.clip(out, 0, 255)
    else:
        lin = _srgb_to_linear(rgb)
        M = _interpolate_matrix(cb_type, severity)
        sim_lin = M @ lin
        sim_lin = np.clip(sim_lin, 0.0, 1.0)
        out = _linear_to_srgb(sim_lin)

    r, g, b = (int(round(v)) for v in out)
    return f"#{r:02X}{g:02X}{b:02X}"


def simulate_mapping(
    hexes: Iterable[str],
    cb_type: CbType,
    severity: float = 1.0,
) -> dict[str, str]:
    """Build ``{original_hex: simulated_hex}`` for a set of colors.

    Skips identity entries (where simulation == original) so the SVG
    writer has fewer no-ops to apply.
    """
    out: dict[str, str] = {}
    for h in hexes:
        h_u = h.upper()
        sim = simulate_hex(h_u, cb_type, severity)
        if sim != h_u:
            out[h_u] = sim
    return out


# --------------------------------------------------------------------------- #
# Risk assessment
# --------------------------------------------------------------------------- #
@dataclass
class RiskAssessment:
    """Per-CB-type findings for one illustration."""

    deutan: bool = False
    protan: bool = False
    tritan: bool = False
    achromat: bool = False
    collapsed_pairs: list[tuple[CbType, str, str, float, float]] = None  # type: ignore[assignment]
    """List of (cb_type, hex_a, hex_b, original_dE, simulated_dE)."""

    def __post_init__(self) -> None:
        if self.collapsed_pairs is None:
            self.collapsed_pairs = []

    @property
    def any_affected(self) -> bool:
        return self.deutan or self.protan or self.tritan or self.achromat

    def affected_types(self) -> list[CbType]:
        out: list[CbType] = []
        for t in CB_TYPES:
            if getattr(self, t):
                out.append(t)
        return out


def _de(hex_a: str, hex_b: str) -> float:
    return delta_e_76(_hex_to_rgb(hex_a), _hex_to_rgb(hex_b))


def assess_risk(
    hexes: Iterable[str],
    *,
    distinct_threshold: float = 25.0,
    collapse_threshold: float = 10.0,
    severity: float = 1.0,
) -> RiskAssessment:
    """Detect CB types that collapse semantically distinct color pairs.

    For every pair of unique colors ``(a, b)`` in ``hexes`` whose
    original ΔE76 exceeds ``distinct_threshold`` (i.e. clearly different
    to a normal-vision observer), we run the simulation for each CB
    type and check whether ΔE76 between simulated colors falls below
    ``collapse_threshold``. If so, the illustration is flagged for
    that CB type.

    The thresholds are defaults — change them via kwargs if a
    particular library has unusually subtle palettes.
    """
    palette = sorted({h.upper() for h in hexes})
    risk = RiskAssessment()
    for i in range(len(palette)):
        for j in range(i + 1, len(palette)):
            a, b = palette[i], palette[j]
            original = _de(a, b)
            if original < distinct_threshold:
                continue
            for cb in CB_TYPES:
                sim_a = simulate_hex(a, cb, severity)
                sim_b = simulate_hex(b, cb, severity)
                sim_de = _de(sim_a, sim_b)
                if sim_de < collapse_threshold:
                    setattr(risk, cb, True)
                    risk.collapsed_pairs.append((cb, a, b, original, sim_de))
    return risk


__all__ = [
    "CB_TYPES",
    "POPULATION_PCT",
    "RiskAssessment",
    "assess_risk",
    "simulate_hex",
    "simulate_mapping",
]
