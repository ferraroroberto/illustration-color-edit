"""
Shared UI helpers for the Streamlit tabs.

Anything that's *Streamlit-specific* but reused across more than one tab
lives here. Pure data logic stays in ``src/``.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.color_mapper import ColorMapper
from src.mapping_store import MappingStore
from src.svg_parser import parse_svg


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
def render_inline_svg(svg_bytes: bytes, *, height: int = 480) -> None:
    """Render raw SVG bytes inline. Strips XML decl so HTML doesn't choke."""
    text = svg_bytes.decode("utf-8", errors="replace")
    if text.lstrip().startswith("<?xml"):
        text = text.split("?>", 1)[1].lstrip()
    wrapper = (
        f'<div style="background:#fff;border:1px solid #e0e0e0;border-radius:6px;'
        f'padding:8px;height:{height}px;overflow:auto;display:flex;'
        f'align-items:center;justify-content:center;">{text}</div>'
    )
    st.markdown(wrapper, unsafe_allow_html=True)


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


# --------------------------------------------------------------------------- #
# Convenience accessors over st.session_state
# --------------------------------------------------------------------------- #
def fresh_mapper() -> ColorMapper:
    """Build a ColorMapper from current config + live global map."""
    cfg = st.session_state.config
    store: MappingStore = st.session_state.store
    return ColorMapper(global_map=store.load_global_map(), matching=cfg.matching)
