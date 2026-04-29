"""
SVG parsing and color extraction.

Surfaces a single dataclass-driven API:

    parse_svg(path | bytes | str) -> ParsedSVG

    ParsedSVG.colors  -> dict[hex -> ColorUsage]   (canonical #RRGGBB uppercase)

The parser walks every element and extracts color tokens from:

  * ``fill`` / ``stroke`` / ``stop-color`` attributes
  * inline ``style="..."`` CSS
  * ``<style>`` blocks (CSS rules)

It normalizes all forms to uppercase 6-digit hex:

  * ``#f00`` -> ``#FF0000``
  * ``red`` -> ``#FF0000``
  * ``rgb(255, 0, 0)`` -> ``#FF0000``
  * ``rgb(100%, 0%, 0%)`` -> ``#FF0000``

Tokens that the SVG color spec treats as non-paint (``none``,
``currentColor``, ``inherit``, ``transparent``, URL refs to gradients) are
ignored — we don't try to remap them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Union

from lxml import etree

log = logging.getLogger(__name__)


# CSS Level 3/4 named colors (the subset SVG uses). Lowercased keys.
# Source: https://www.w3.org/TR/css-color-4/#named-colors
CSS_NAMED_COLORS: dict[str, str] = {
    "aliceblue": "#F0F8FF", "antiquewhite": "#FAEBD7", "aqua": "#00FFFF",
    "aquamarine": "#7FFFD4", "azure": "#F0FFFF", "beige": "#F5F5DC",
    "bisque": "#FFE4C4", "black": "#000000", "blanchedalmond": "#FFEBCD",
    "blue": "#0000FF", "blueviolet": "#8A2BE2", "brown": "#A52A2A",
    "burlywood": "#DEB887", "cadetblue": "#5F9EA0", "chartreuse": "#7FFF00",
    "chocolate": "#D2691E", "coral": "#FF7F50", "cornflowerblue": "#6495ED",
    "cornsilk": "#FFF8DC", "crimson": "#DC143C", "cyan": "#00FFFF",
    "darkblue": "#00008B", "darkcyan": "#008B8B", "darkgoldenrod": "#B8860B",
    "darkgray": "#A9A9A9", "darkgreen": "#006400", "darkgrey": "#A9A9A9",
    "darkkhaki": "#BDB76B", "darkmagenta": "#8B008B", "darkolivegreen": "#556B2F",
    "darkorange": "#FF8C00", "darkorchid": "#9932CC", "darkred": "#8B0000",
    "darksalmon": "#E9967A", "darkseagreen": "#8FBC8F", "darkslateblue": "#483D8B",
    "darkslategray": "#2F4F4F", "darkslategrey": "#2F4F4F",
    "darkturquoise": "#00CED1", "darkviolet": "#9400D3", "deeppink": "#FF1493",
    "deepskyblue": "#00BFFF", "dimgray": "#696969", "dimgrey": "#696969",
    "dodgerblue": "#1E90FF", "firebrick": "#B22222", "floralwhite": "#FFFAF0",
    "forestgreen": "#228B22", "fuchsia": "#FF00FF", "gainsboro": "#DCDCDC",
    "ghostwhite": "#F8F8FF", "gold": "#FFD700", "goldenrod": "#DAA520",
    "gray": "#808080", "green": "#008000", "greenyellow": "#ADFF2F",
    "grey": "#808080", "honeydew": "#F0FFF0", "hotpink": "#FF69B4",
    "indianred": "#CD5C5C", "indigo": "#4B0082", "ivory": "#FFFFF0",
    "khaki": "#F0E68C", "lavender": "#E6E6FA", "lavenderblush": "#FFF0F5",
    "lawngreen": "#7CFC00", "lemonchiffon": "#FFFACD", "lightblue": "#ADD8E6",
    "lightcoral": "#F08080", "lightcyan": "#E0FFFF",
    "lightgoldenrodyellow": "#FAFAD2", "lightgray": "#D3D3D3",
    "lightgreen": "#90EE90", "lightgrey": "#D3D3D3", "lightpink": "#FFB6C1",
    "lightsalmon": "#FFA07A", "lightseagreen": "#20B2AA",
    "lightskyblue": "#87CEFA", "lightslategray": "#778899",
    "lightslategrey": "#778899", "lightsteelblue": "#B0C4DE",
    "lightyellow": "#FFFFE0", "lime": "#00FF00", "limegreen": "#32CD32",
    "linen": "#FAF0E6", "magenta": "#FF00FF", "maroon": "#800000",
    "mediumaquamarine": "#66CDAA", "mediumblue": "#0000CD",
    "mediumorchid": "#BA55D3", "mediumpurple": "#9370DB",
    "mediumseagreen": "#3CB371", "mediumslateblue": "#7B68EE",
    "mediumspringgreen": "#00FA9A", "mediumturquoise": "#48D1CC",
    "mediumvioletred": "#C71585", "midnightblue": "#191970",
    "mintcream": "#F5FFFA", "mistyrose": "#FFE4E1", "moccasin": "#FFE4B5",
    "navajowhite": "#FFDEAD", "navy": "#000080", "oldlace": "#FDF5E6",
    "olive": "#808000", "olivedrab": "#6B8E23", "orange": "#FFA500",
    "orangered": "#FF4500", "orchid": "#DA70D6", "palegoldenrod": "#EEE8AA",
    "palegreen": "#98FB98", "paleturquoise": "#AFEEEE",
    "palevioletred": "#DB7093", "papayawhip": "#FFEFD5", "peachpuff": "#FFDAB9",
    "peru": "#CD853F", "pink": "#FFC0CB", "plum": "#DDA0DD",
    "powderblue": "#B0E0E6", "purple": "#800080", "rebeccapurple": "#663399",
    "red": "#FF0000", "rosybrown": "#BC8F8F", "royalblue": "#4169E1",
    "saddlebrown": "#8B4513", "salmon": "#FA8072", "sandybrown": "#F4A460",
    "seagreen": "#2E8B57", "seashell": "#FFF5EE", "sienna": "#A0522D",
    "silver": "#C0C0C0", "skyblue": "#87CEEB", "slateblue": "#6A5ACD",
    "slategray": "#708090", "slategrey": "#708090", "snow": "#FFFAFA",
    "springgreen": "#00FF7F", "steelblue": "#4682B4", "tan": "#D2B48C",
    "teal": "#008080", "thistle": "#D8BFD8", "tomato": "#FF6347",
    "turquoise": "#40E0D0", "violet": "#EE82EE", "wheat": "#F5DEB3",
    "white": "#FFFFFF", "whitesmoke": "#F5F5F5", "yellow": "#FFFF00",
    "yellowgreen": "#9ACD32",
}

# Tokens that SVG accepts as paint values but that are not concrete colors —
# we leave them alone during extraction *and* mapping.
NON_COLOR_TOKENS = frozenset({"none", "transparent", "currentcolor", "inherit", "initial", "unset"})

# Attributes whose value is always a paint reference.
PAINT_ATTRS = ("fill", "stroke", "stop-color", "flood-color", "lighting-color")

# CSS properties whose value is a paint reference.
CSS_PAINT_PROPS = frozenset({
    "fill", "stroke", "stop-color", "flood-color", "lighting-color", "color",
    "background", "background-color", "border-color",
})

_HEX_RE = re.compile(r"#([0-9a-fA-F]{3,8})\b")
_RGB_RE = re.compile(
    r"rgba?\(\s*"
    r"(-?\d+(?:\.\d+)?%?)\s*,?\s*"
    r"(-?\d+(?:\.\d+)?%?)\s*,?\s*"
    r"(-?\d+(?:\.\d+)?%?)"
    r"(?:\s*[,/]\s*-?\d+(?:\.\d+)?%?)?"  # alpha (ignored)
    r"\s*\)",
    re.IGNORECASE,
)
_NAMED_RE = re.compile(r"\b([a-zA-Z]{3,30})\b")


@dataclass
class ColorUsage:
    """Tracks where a normalized color appears inside an SVG."""

    hex: str  # canonical #RRGGBB uppercase
    count: int = 0
    contexts: set[str] = field(default_factory=set)
    raw_forms: set[str] = field(default_factory=set)

    def record(self, context: str, raw: str) -> None:
        self.count += 1
        self.contexts.add(context)
        self.raw_forms.add(raw)


@dataclass
class ParsedSVG:
    """Parsed SVG with extracted color usage. ``tree`` is the raw lxml tree."""

    source: Optional[Path]
    tree: etree._ElementTree
    colors: dict[str, ColorUsage]
    raw_bytes: bytes

    @property
    def unique_color_count(self) -> int:
        return len(self.colors)


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
def normalize_hex(value: str) -> Optional[str]:
    """
    Normalize a single CSS/SVG color token to canonical ``#RRGGBB`` uppercase.

    Returns ``None`` if the value is not a concrete color (``none``,
    ``currentColor``, gradient URL, malformed input, etc.).
    """
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    low = v.lower()
    if low in NON_COLOR_TOKENS:
        return None
    if low.startswith("url("):
        return None

    # Hex
    if v.startswith("#"):
        body = v[1:]
        if len(body) == 3 and all(c in "0123456789abcdefABCDEF" for c in body):
            return "#" + "".join(ch * 2 for ch in body).upper()
        if len(body) == 4 and all(c in "0123456789abcdefABCDEF" for c in body):
            # #RGBA -> drop alpha, expand
            return "#" + "".join(ch * 2 for ch in body[:3]).upper()
        if len(body) == 6 and all(c in "0123456789abcdefABCDEF" for c in body):
            return "#" + body.upper()
        if len(body) == 8 and all(c in "0123456789abcdefABCDEF" for c in body):
            return "#" + body[:6].upper()
        return None

    # rgb(...) / rgba(...)
    m = _RGB_RE.fullmatch(v)
    if m:
        try:
            channels = [_parse_channel(m.group(i)) for i in (1, 2, 3)]
        except ValueError:
            return None
        return "#{:02X}{:02X}{:02X}".format(*channels)

    # Named color
    if low in CSS_NAMED_COLORS:
        return CSS_NAMED_COLORS[low]

    return None


def _parse_channel(token: str) -> int:
    token = token.strip()
    if token.endswith("%"):
        pct = float(token[:-1])
        return max(0, min(255, round(pct * 255 / 100)))
    val = float(token)
    return max(0, min(255, round(val)))


def iter_color_tokens(text: str) -> Iterable[tuple[str, str]]:
    """
    Yield ``(raw_token, normalized_hex)`` pairs found anywhere in ``text``.

    Used both for inline ``style`` attributes and for ``<style>`` block
    contents. We scan with regexes and normalize each match; tokens that
    don't normalize (e.g. ``url(#grad1)``) are skipped.
    """
    seen_spans: set[tuple[int, int]] = set()

    for m in _HEX_RE.finditer(text):
        span = m.span()
        seen_spans.add(span)
        norm = normalize_hex(m.group(0))
        if norm:
            yield m.group(0), norm

    for m in _RGB_RE.finditer(text):
        seen_spans.add(m.span())
        norm = normalize_hex(m.group(0))
        if norm:
            yield m.group(0), norm

    for m in _NAMED_RE.finditer(text):
        # Skip if this match overlaps with an already-consumed hex/rgb token
        # (e.g. the "rgb" inside "rgb(...)").
        s, e = m.span()
        if any(not (e <= ss or s >= ee) for ss, ee in seen_spans):
            continue
        word = m.group(1).lower()
        if word in CSS_NAMED_COLORS:
            yield m.group(1), CSS_NAMED_COLORS[word]


# --------------------------------------------------------------------------- #
# Style attribute parsing
# --------------------------------------------------------------------------- #
_DECL_RE = re.compile(r"\s*([a-zA-Z-]+)\s*:\s*([^;]+?)\s*(?:;|$)")


def parse_style_attribute(value: str) -> list[tuple[str, str]]:
    """
    Parse an inline ``style="prop: val; prop: val"`` attribute into a list of
    ``(property, value)`` pairs, preserving order.
    """
    if not value:
        return []
    return [(m.group(1).strip().lower(), m.group(2).strip()) for m in _DECL_RE.finditer(value)]


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
SVG_NS = "http://www.w3.org/2000/svg"


def _localname(tag: object) -> str:
    """Return the local part of an etree tag (strip namespace)."""
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def parse_svg(source: Union[str, Path, bytes]) -> ParsedSVG:
    """
    Parse an SVG and return a :class:`ParsedSVG`.

    ``source`` may be a path-like, a raw bytes blob, or an SVG string.
    """
    raw: bytes
    src_path: Optional[Path]
    if isinstance(source, (str, Path)) and not (
        isinstance(source, str) and source.lstrip().startswith("<")
    ):
        src_path = Path(source)
        raw = src_path.read_bytes()
    elif isinstance(source, bytes):
        src_path = None
        raw = source
    else:
        src_path = None
        raw = source.encode("utf-8")  # type: ignore[union-attr]

    parser = etree.XMLParser(remove_comments=False, recover=False, huge_tree=True)
    try:
        tree = etree.ElementTree(etree.fromstring(raw, parser=parser))
    except etree.XMLSyntaxError as exc:
        log.error("Malformed SVG (%s): %s", src_path, exc)
        raise

    colors: dict[str, ColorUsage] = {}

    def _record(norm: str, context: str, raw_form: str) -> None:
        cu = colors.setdefault(norm, ColorUsage(hex=norm))
        cu.record(context, raw_form)

    root = tree.getroot()
    for el in root.iter():
        local = _localname(el.tag)

        # Paint attributes
        for attr in PAINT_ATTRS:
            val = el.get(attr)
            if val is None:
                continue
            norm = normalize_hex(val)
            if norm:
                _record(norm, f"@{attr}", val)

        # Inline style
        style = el.get("style")
        if style:
            for prop, val in parse_style_attribute(style):
                if prop in CSS_PAINT_PROPS:
                    norm = normalize_hex(val)
                    if norm:
                        _record(norm, f"style.{prop}", val)
                    else:
                        # value may itself contain a token (e.g. "url(...) #fff")
                        for raw_tok, n in iter_color_tokens(val):
                            _record(n, f"style.{prop}", raw_tok)

        # <style> blocks: scan their text content for any color tokens.
        if local == "style":
            text = "".join(el.itertext())
            for raw_tok, norm in iter_color_tokens(text):
                _record(norm, "style-block", raw_tok)

    return ParsedSVG(source=src_path, tree=tree, colors=colors, raw_bytes=raw)


def extract_unique_colors(source: Union[str, Path, bytes]) -> dict[str, int]:
    """Convenience: return ``{normalized_hex: count}`` for an SVG."""
    parsed = parse_svg(source)
    return {h: u.count for h, u in parsed.colors.items()}
