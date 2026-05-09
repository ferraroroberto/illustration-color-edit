"""Detect fine black strokes and small black text in source SVGs.

Press requirement: thin black lines and small black text should print as
**100% K only** (pure black ink). When they're rendered as four-color
black (a CMYK mix), tiny misregistration between plates produces visible
colored fringing on press — common cause of cosmetically-bad books.

This module gives the pipeline a *detection* pass: walk the SVG, find
strokes/text that should be K-only by virtue of their stroke width or
font size, and report counts + samples. The audit sidecar and QA
report surface the result so the illustrator can either edit the
artwork or opt the file in to the auto-fix path (Ghostscript's
``-dBlackText=true -dBlackVector=true`` flags during the RGB→CMYK pass).

We don't try to *force* the conversion in here — the rewrite is risky
on Inkscape-produced PDFs and the Ghostscript flags do the right thing
when enabled. This module just reports what the user can't see by eye.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from lxml import etree

from .cmyk_gamut import delta_e_76
from .svg_parser import _localname, normalize_hex, parse_style_attribute

log = logging.getLogger(__name__)


# Black-or-near-black: anything within ΔE76 ≤ 15 of pure #000000 in Lab.
# 15 captures #1A1A1A and slightly darker — colors a designer "meant"
# to be black even if not exact #000000. Above 15 we leave alone (the
# user picked a real gray on purpose).
_NEAR_BLACK_DELTA_E = 15.0
_PURE_BLACK = (0, 0, 0)


@dataclass
class FineLineHit:
    """One detected fine-line or small-text element."""

    element: str          # localname: "path", "text", "rect", ...
    kind: str             # "stroke" | "text"
    color_hex: str        # the (near-black) color found
    size_pt: float        # stroke width (or font size) in points at trim scale
    sample: str = ""      # short snippet for the audit report


@dataclass
class FineLineReport:
    """Aggregate finding for one SVG."""

    stroke_count: int = 0
    text_count: int = 0
    samples: list[FineLineHit] = field(default_factory=list)
    """Up to ~10 samples for the audit sidecar; more are summarized as a count."""

    @property
    def total(self) -> int:
        return self.stroke_count + self.text_count

    def summary(self) -> str:
        """Compact one-line summary for the audit report."""
        if self.total == 0:
            return "(none)"
        parts = []
        if self.stroke_count:
            parts.append(
                f"{self.stroke_count} fine stroke"
                f"{'s' if self.stroke_count != 1 else ''}"
            )
        if self.text_count:
            parts.append(
                f"{self.text_count} small text element"
                f"{'s' if self.text_count != 1 else ''}"
            )
        return ", ".join(parts)


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
_LENGTH_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)([a-zA-Z%]*)\s*$")


def _parse_length(value: str) -> Optional[tuple[float, str]]:
    """Return ``(number, unit)`` for an SVG length, or ``None`` if unparseable.

    Empty unit means "user units" (the SVG default). Recognized units
    are passed through verbatim — the caller decides how to convert.
    """
    if not value:
        return None
    m = _LENGTH_RE.match(value)
    if not m:
        return None
    return float(m.group(1)), m.group(2).lower()


def _viewbox(root: etree._Element) -> Optional[tuple[float, float, float, float]]:
    vb = root.get("viewBox")
    if not vb:
        return None
    parts = vb.replace(",", " ").split()
    if len(parts) != 4:
        return None
    try:
        return tuple(float(p) for p in parts)  # type: ignore[return-value]
    except ValueError:
        return None


def _user_units_per_pt(root: etree._Element, trim_inches: tuple[float, float]) -> float:
    """Return the number of SVG user units that correspond to one printer's point.

    Derived from the trim size and the viewBox: ``user_units_per_inch =
    viewbox_width / trim_w``, then divided by 72 (points per inch). Falls
    back to 96/72 = 4/3 (the CSS px→pt ratio Inkscape uses when there's
    no viewBox) so the function still returns a usable value on
    malformed SVGs.
    """
    vb = _viewbox(root)
    trim_w, _ = trim_inches
    if vb and trim_w > 0:
        _, _, vw, _ = vb
        if vw > 0:
            return (vw / trim_w) / 72.0
    return 96.0 / 72.0  # ~1.333 user units per pt — Inkscape's default.


def _convert_length_to_pt(
    raw: str,
    user_units_per_pt: float,
) -> Optional[float]:
    """Convert an SVG length string to printer's points at trim scale.

    Recognized units: ``""`` / ``px`` (user units), ``pt``, ``mm``,
    ``cm``, ``in``. ``%`` is ignored (we have no parent box). Returns
    ``None`` when the length can't be parsed or has an unsupported unit.
    """
    parsed = _parse_length(raw)
    if parsed is None:
        return None
    n, unit = parsed
    if unit in ("", "px"):
        return n / user_units_per_pt if user_units_per_pt > 0 else None
    if unit == "pt":
        return n
    if unit == "mm":
        return n * 72.0 / 25.4
    if unit == "cm":
        return n * 72.0 / 2.54
    if unit == "in":
        return n * 72.0
    return None


# --------------------------------------------------------------------------- #
# Color helpers
# --------------------------------------------------------------------------- #
def _is_near_black(hex_color: str) -> bool:
    h = normalize_hex(hex_color)
    if not h:
        return False
    rgb = (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))
    return delta_e_76(rgb, _PURE_BLACK) <= _NEAR_BLACK_DELTA_E


def _resolve_color(
    el: etree._Element,
    prop: str,
    style_pairs: list[tuple[str, str]],
) -> Optional[str]:
    """Resolve a paint property's value: inline attr, then inline style.

    SVG inheritance is *not* followed — for the "is this near black?"
    question, missing means missing (or rather "use the renderer
    default of black", which we treat as black for stroke detection
    only inside ``_walk_strokes``).
    """
    val = el.get(prop)
    if val:
        h = normalize_hex(val)
        if h:
            return h
    for sprop, sval in style_pairs:
        if sprop == prop:
            h = normalize_hex(sval)
            if h:
                return h
    return None


def _resolve_length(
    el: etree._Element,
    prop: str,
    style_pairs: list[tuple[str, str]],
) -> Optional[str]:
    val = el.get(prop)
    if val:
        return val
    for sprop, sval in style_pairs:
        if sprop == prop:
            return sval
    return None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
_STROKED_ELEMENTS = frozenset({
    "path", "line", "polyline", "polygon", "rect", "circle", "ellipse",
})


def find_fine_lines(
    svg_source: Path | bytes | str,
    trim_inches: tuple[float, float],
    *,
    min_stroke_pt: float = 0.5,
    min_text_pt: float = 9.0,
    sample_limit: int = 10,
) -> FineLineReport:
    """Scan an SVG for near-black strokes/text below the given thresholds.

    :param svg_source: file path, bytes, or SVG string.
    :param trim_inches: ``(width, height)`` of the trim box; used to
        translate user-unit stroke widths into points at the printed
        scale. Pass the same values used by the pipeline (without bleed).
    :param min_stroke_pt: strokes ≤ this in points are flagged.
        0.5 pt is a common publisher minimum; tighten to 0.25 pt for
        very fine illustration work.
    :param min_text_pt: text with ``font-size`` ≤ this in points is
        flagged. 9 pt is conservative for body-style annotation;
        publishers often want force-K up to 12 pt.

    No-op for a non-SVG / unparseable input — returns an empty report.
    """
    if isinstance(svg_source, (str, Path)) and not (
        isinstance(svg_source, str) and svg_source.lstrip().startswith("<")
    ):
        try:
            tree = etree.parse(str(svg_source))
        except (OSError, etree.XMLSyntaxError):
            return FineLineReport()
        root = tree.getroot()
    else:
        try:
            raw = (
                svg_source if isinstance(svg_source, bytes)
                else svg_source.encode("utf-8")  # type: ignore[union-attr]
            )
            root = etree.fromstring(raw)
        except etree.XMLSyntaxError:
            return FineLineReport()

    uupp = _user_units_per_pt(root, trim_inches)
    report = FineLineReport()

    for el in root.iter():
        local = _localname(el.tag)
        style_attr = el.get("style")
        style_pairs = parse_style_attribute(style_attr) if style_attr else []

        # ----- stroke check -------------------------------------------------- #
        if local in _STROKED_ELEMENTS:
            stroke_color = _resolve_color(el, "stroke", style_pairs)
            if stroke_color and _is_near_black(stroke_color):
                width_raw = _resolve_length(el, "stroke-width", style_pairs)
                if width_raw is None:
                    width_pt = 1.0 / uupp  # SVG default = 1 user unit
                else:
                    width_pt = _convert_length_to_pt(width_raw, uupp) or 0.0
                if 0 < width_pt <= min_stroke_pt:
                    report.stroke_count += 1
                    if len(report.samples) < sample_limit:
                        report.samples.append(FineLineHit(
                            element=local,
                            kind="stroke",
                            color_hex=stroke_color,
                            size_pt=round(width_pt, 3),
                            sample=f"<{local} stroke={stroke_color} stroke-width={width_raw or '(default)'}>",
                        ))

        # ----- text check ---------------------------------------------------- #
        if local == "text":
            fill_color = _resolve_color(el, "fill", style_pairs) or "#000000"
            if not _is_near_black(fill_color):
                continue
            size_raw = _resolve_length(el, "font-size", style_pairs)
            if size_raw is None:
                # SVG/CSS default is 16px = 16 user units. Translate to pt.
                size_pt = 16.0 / uupp
            else:
                size_pt = _convert_length_to_pt(size_raw, uupp) or 0.0
            if 0 < size_pt <= min_text_pt:
                report.text_count += 1
                if len(report.samples) < sample_limit:
                    snippet = "".join(el.itertext()).strip()
                    if len(snippet) > 30:
                        snippet = snippet[:27] + "…"
                    report.samples.append(FineLineHit(
                        element="text",
                        kind="text",
                        color_hex=fill_color,
                        size_pt=round(size_pt, 3),
                        sample=f'<text fill={fill_color} font-size={size_raw or "(default)"}>"{snippet}"</text>',
                    ))

    return report


__all__ = ["FineLineHit", "FineLineReport", "find_fine_lines"]
