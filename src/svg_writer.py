"""
SVG writer — applies a color mapping to a parsed SVG.

Strategy: re-walk the tree the parser produced, find color tokens in the
same locations (paint attributes, inline ``style`` CSS, ``<style>`` block
text), and substitute them in place. Everything else (paths, transforms,
text, IDs, comments, namespaces) is left untouched so the round-trip back
into Affinity Designer is clean.

The mapping passed in is a flat dict of canonical ``#RRGGBB`` -> target
``#RRGGBB``. Colors not in the mapping are left as-is (callers can decide
to flag them; see :func:`apply_mapping_with_report`).
"""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from lxml import etree

from .svg_parser import (
    CSS_PAINT_PROPS,
    PAINT_ATTRS,
    ParsedSVG,
    _HEX_RE,
    _NAMED_RE,
    _RGB_RE,
    _localname,
    normalize_hex,
    parse_style_attribute,
    parse_svg,
)

log = logging.getLogger(__name__)


@dataclass
class WriteReport:
    """Summary returned by :func:`apply_mapping_with_report`."""

    replacements: int = 0                              # total tokens rewritten
    by_source: dict[str, int] = field(default_factory=dict)  # per-source-hex counts
    unmapped: dict[str, int] = field(default_factory=dict)   # source hexes with no mapping

    def record_replace(self, source_hex: str) -> None:
        self.replacements += 1
        self.by_source[source_hex] = self.by_source.get(source_hex, 0) + 1

    def record_unmapped(self, source_hex: str) -> None:
        self.unmapped[source_hex] = self.unmapped.get(source_hex, 0) + 1


# --------------------------------------------------------------------------- #
# Token-level replacement (used for inline-style values and <style> blocks)
# --------------------------------------------------------------------------- #
_COMBINED_RE = re.compile(
    r"(rgba?\(\s*[^)]*\))"               # rgb(...) / rgba(...)
    r"|(#[0-9a-fA-F]{3,8}\b)"            # hex
    r"|\b([a-zA-Z]{3,30})\b",            # word (possibly a named color)
)


def replace_color_tokens(
    text: str,
    mapping: dict[str, str],
    on_replace=None,
    on_unmapped=None,
) -> str:
    """
    Replace every concrete color token in ``text`` whose normalized form is a
    key in ``mapping``. Tokens that don't normalize to a known color (e.g.
    ``url(#g)``, the bare word ``fill``) are left alone.

    Callbacks (optional) let the caller track replacements and unmapped
    colors for reporting.
    """
    def _sub(m: re.Match) -> str:
        token = m.group(0)
        norm = normalize_hex(token)
        if norm is None:
            return token  # not a color we recognise — leave it alone
        target = mapping.get(norm)
        if target is None:
            if on_unmapped is not None:
                on_unmapped(norm)
            return token
        if on_replace is not None:
            on_replace(norm)
        return target
    return _COMBINED_RE.sub(_sub, text)


# --------------------------------------------------------------------------- #
# Element-level rewriting
# --------------------------------------------------------------------------- #
def _rewrite_inline_style(
    style_value: str,
    mapping: dict[str, str],
    report: WriteReport,
) -> str:
    """Rewrite an inline ``style="..."`` attribute, only touching paint props."""
    decls = parse_style_attribute(style_value)
    if not decls:
        return style_value

    out_parts: list[str] = []
    for prop, val in decls:
        if prop in CSS_PAINT_PROPS:
            new_val = replace_color_tokens(
                val,
                mapping,
                on_replace=report.record_replace,
                on_unmapped=report.record_unmapped,
            )
            out_parts.append(f"{prop}: {new_val}")
        else:
            out_parts.append(f"{prop}: {val}")
    return "; ".join(out_parts)


def _rewrite_element(
    el: etree._Element,
    mapping: dict[str, str],
    report: WriteReport,
) -> None:
    # Paint attributes
    for attr in PAINT_ATTRS:
        val = el.get(attr)
        if val is None:
            continue
        norm = normalize_hex(val)
        if norm is None:
            continue
        target = mapping.get(norm)
        if target is None:
            report.record_unmapped(norm)
            continue
        el.set(attr, target)
        report.record_replace(norm)

    # Inline style
    style = el.get("style")
    if style:
        new_style = _rewrite_inline_style(style, mapping, report)
        if new_style != style:
            el.set("style", new_style)

    # <style> block text
    if _localname(el.tag) == "style":
        if el.text:
            new_text = replace_color_tokens(
                el.text,
                mapping,
                on_replace=report.record_replace,
                on_unmapped=report.record_unmapped,
            )
            if new_text != el.text:
                el.text = new_text
        # Some authoring tools wrap CSS in CDATA which lxml exposes as a child
        # node; iterate child text just in case.
        for child in el:
            if child.text:
                new_child_text = replace_color_tokens(
                    child.text, mapping,
                    on_replace=report.record_replace,
                    on_unmapped=report.record_unmapped,
                )
                if new_child_text != child.text:
                    child.text = new_child_text


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def apply_mapping_with_report(
    source: Union[ParsedSVG, str, Path, bytes],
    mapping: dict[str, str],
) -> tuple[bytes, WriteReport]:
    """
    Apply ``mapping`` (source_hex -> target_hex; canonical ``#RRGGBB``) to
    ``source`` and return ``(svg_bytes, report)``.

    ``source`` may be a :class:`ParsedSVG` (re-used to avoid re-parsing) or
    anything :func:`parse_svg` accepts.
    """
    if isinstance(source, ParsedSVG):
        # Operate on a deep copy so the input ParsedSVG stays clean.
        tree = deepcopy(source.tree)
    else:
        tree = parse_svg(source).tree
        tree = deepcopy(tree)

    norm_mapping = {k.upper(): v.upper() for k, v in mapping.items()}
    report = WriteReport()

    root = tree.getroot()
    for el in root.iter():
        _rewrite_element(el, norm_mapping, report)

    body = etree.tostring(tree, xml_declaration=True, encoding="utf-8", standalone=False)
    return body, report


def apply_mapping(
    source: Union[ParsedSVG, str, Path, bytes],
    mapping: dict[str, str],
) -> bytes:
    """Convenience wrapper around :func:`apply_mapping_with_report`."""
    body, _ = apply_mapping_with_report(source, mapping)
    return body


def write_png_from_svg(
    svg_path: Path,
    destination: Path,
    dpi: int = 300,
    inkscape_exe: str = "inkscape",
) -> None:
    """Rasterise an SVG file to a PNG at ``destination`` via Inkscape CLI."""
    import subprocess

    destination.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        inkscape_exe,
        str(svg_path),
        "--export-type=png",
        f"--export-dpi={dpi}",
        f"--export-filename={destination}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Inkscape failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    log.info("Wrote PNG %s at %d dpi", destination, dpi)


def write_converted_svg(
    source: Union[ParsedSVG, str, Path],
    mapping: dict[str, str],
    destination: Path,
) -> WriteReport:
    """
    Apply ``mapping`` and write the result to ``destination``.

    Creates parent directories. Returns a :class:`WriteReport` for logging.
    """
    body, report = apply_mapping_with_report(source, mapping)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)
    log.info(
        "Wrote %s (%d replacements, %d unmapped colors)",
        destination, report.replacements, len(report.unmapped),
    )
    return report
