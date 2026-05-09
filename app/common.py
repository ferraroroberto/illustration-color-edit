"""
Shared UI helpers for the Streamlit tabs.

Anything that's *Streamlit-specific* but reused across more than one tab
lives here. Pure data logic stays in ``src/``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import streamlit as st

from src.color_mapper import ColorMapper, hex_to_lab
from src.mapping_store import MappingStore
from src.palette import HUE_FAMILIES, hue_family
from src.svg_parser import parse_svg

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Hex parsing helpers (shared by both Editor tabs)
# --------------------------------------------------------------------------- #
_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def normalize_hex(raw: str) -> str | None:
    """Return canonical ``#RRGGBB`` (uppercase) or ``None`` if invalid."""
    m = _HEX_RE.match(raw.strip())
    return f"#{m.group(1).upper()}" if m else None


def apply_hex_input(hk: str, pk: str) -> None:
    """``on_change`` callback: copy a valid hex from a text input into a color picker.

    Used by both editors so users can type ``#3F8B5A`` into the small
    text field and have the adjacent ``st.color_picker`` follow.
    """
    normalized = normalize_hex(st.session_state.get(hk, ""))
    if normalized:
        st.session_state[pk] = normalized


_FAMILY_INDEX = {f: i for i, f in enumerate(HUE_FAMILIES)}


def color_sort_key(hex_color: str) -> tuple[int, float, float, float]:
    """Sort key that groups colors by hue family, then by lightness.

    Used to order the per-color rows in both editors so all reds sit
    together, all whites together, etc. Family order matches
    :data:`src.palette.HUE_FAMILIES` (red → orange → yellow → green →
    cyan → blue → purple → neutral). Within a family, sort by Lab L*
    (dark to light) and then by chroma to break ties deterministically.
    """
    family = hue_family(hex_color)
    L, a, b = hex_to_lab(hex_color)
    return (_FAMILY_INDEX.get(family, len(HUE_FAMILIES)), L, a, b)


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def cached_color_extract(path_str: str, mtime: float) -> dict[str, int]:
    """Return ``{hex: usage_count}`` for an SVG, keyed by path + mtime."""
    parsed = parse_svg(Path(path_str))
    return {h: u.count for h, u in parsed.colors.items()}


# --------------------------------------------------------------------------- #
# Inline rendering
# --------------------------------------------------------------------------- #
def render_inline_svg(
    svg_bytes: bytes,
    *,
    height: int = 480,
    aspect: str | None = None,
) -> None:
    """Render raw SVG bytes inline. Strips XML decl so HTML doesn't choke.

    If ``aspect`` is supplied (e.g. ``"1/1"``), the container uses
    ``width:100%`` + ``aspect-ratio`` instead of a fixed pixel height —
    useful when stacking previews in equal-width columns and you want
    them to share visible size with adjacent ``st.image`` panels.
    """
    text = svg_bytes.decode("utf-8", errors="replace")
    if text.lstrip().startswith("<?xml"):
        text = text.split("?>", 1)[1].lstrip()
    if aspect:
        size_style = f"width:100%;aspect-ratio:{aspect};"
    else:
        size_style = f"height:{height}px;"
    wrapper = (
        f'<div style="background:#fff;border:1px solid #e0e0e0;border-radius:6px;'
        f'padding:8px;{size_style}overflow:auto;display:flex;'
        f'align-items:center;justify-content:center;">{text}</div>'
    )
    st.markdown(wrapper, unsafe_allow_html=True)


def open_in_explorer(path: Path) -> tuple[bool, str]:
    """Open ``path`` in the OS file browser. Returns ``(success, message)``.

    Cross-platform: Windows uses ``os.startfile``, macOS uses ``open``,
    Linux uses ``xdg-open``. The UI calls this from a button; failures
    are surfaced to the user via the returned message rather than raising.
    """
    p = Path(path)
    if not p.exists():
        return False, f"Path does not exist: {p}"
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True, f"Opened {p}"
    except (OSError, FileNotFoundError) as exc:
        log.warning("open_in_explorer failed for %s: %s", p, exc)
        return False, f"Could not open {p}: {exc}"


def status_badge(status: str) -> str:
    color = {
        "pending": "#9CA3AF",
        "in_progress": "#F59E0B",
        "reviewed": "#10B981",
        "exported": "#3B82F6",
    }.get(status, "#9CA3AF")
    return (
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-size:0.8em;">{status}</span>'
    )


def color_swatch(hex_color: str, size: int = 22) -> str:
    return (
        f'<span style="display:inline-block;width:{size}px;height:{size}px;'
        f'background:{hex_color};border:1px solid #aaa;border-radius:3px;'
        f'vertical-align:middle;"></span>'
    )


_STATUS_COLORS = {
    "pending": "#9CA3AF",
    "in_progress": "#F59E0B",
    "reviewed": "#10B981",
    "exported": "#3B82F6",
}


def compact_status_counters(label: str, counts: dict[str, int]) -> str:
    """Compact one-line "label  ● 0  ● 1  ● 0  ● 0" status counter row.

    Replaces the older pill-badge layout that wrapped awkwardly in narrow
    columns. Each status is a small colored dot followed by its count;
    statuses sit on a single non-wrapping line via ``white-space:nowrap``.
    """
    parts = [
        f'<span style="font-weight:600;margin-right:10px;">{label}</span>'
    ]
    for status in ("pending", "in_progress", "reviewed", "exported"):
        c = _STATUS_COLORS[status]
        parts.append(
            f'<span style="margin-right:14px;white-space:nowrap;" '
            f'title="{status}">'
            f'<span style="display:inline-block;width:8px;height:8px;'
            f'border-radius:50%;background:{c};margin-right:5px;'
            f'vertical-align:middle;"></span>'
            f'<span style="color:#cfcfcf;font-size:0.85em;">{status}</span>'
            f'<span style="margin-left:6px;font-variant-numeric:tabular-nums;">'
            f'{counts.get(status, 0)}</span>'
            f'</span>'
        )
    return (
        f'<div style="display:flex;flex-wrap:wrap;align-items:center;'
        f'line-height:1.8;">{"".join(parts)}</div>'
    )


# --------------------------------------------------------------------------- #
# Convenience accessors over st.session_state
# --------------------------------------------------------------------------- #
def fresh_mapper() -> ColorMapper:
    """Build a ColorMapper from current config + live global map."""
    cfg = st.session_state.config
    store: MappingStore = st.session_state.store
    return ColorMapper(global_map=store.load_global_map(), matching=cfg.matching)
