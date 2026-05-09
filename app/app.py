"""
Streamlit entry point.

Scaffolding only — bootstraps config + session state, then routes the
left-sidebar navigation to ``app/tab_*.py``. All real logic lives in
``src/``; all per-tab UI lives in its own ``tab_*.py`` file.

Navigation is grouped into three sections (Library / Grayscale / CMYK)
because with ~9 destinations a top tab strip wraps awkwardly. Sidebar
buttons give us clear grouping and an explicit "active" indicator via the
primary/secondary button styling.

Run with::

    streamlit run app/app.py

or via the Windows wrapper::

    launch_app.bat
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make src/ importable and tab modules importable without going through the
# app package (avoids a circular-import in Streamlit >= 1.41 where the runner
# registers the script as sys.modules['app'] before the body finishes).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_APP_DIR = Path(__file__).resolve().parent
for _p in (_PROJECT_ROOT, _APP_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import streamlit as st  # noqa: E402

from src.config import configure_logging, load_config  # noqa: E402
from src.library_manager import LibraryManager  # noqa: E402
from src.mapping_store import MappingStore  # noqa: E402

import tab_batch, tab_editor, tab_global_map, tab_library, tab_settings  # noqa: E402, E401
import tab_cmyk_editor, tab_cmyk_export, tab_cmyk_global_map, tab_cmyk_settings  # noqa: E402, E401
import tab_palette  # noqa: E402


st.set_page_config(layout="wide", page_title="Illustration Color Edit")

# Tighten the default top padding so the page header sits closer to the top.
st.markdown(
    """
    <style>
        .block-container { padding-top: 1.5rem; }
        section[data-testid="stSidebar"] .block-container { padding-top: 1.5rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _bootstrap() -> None:
    """Initialize config + persistent stores once per session."""
    if "config" in st.session_state:
        return
    cfg = load_config()
    cfg.ensure_dirs()
    configure_logging(cfg.log_level)

    cfg_path = cfg.source_path or (_PROJECT_ROOT / "config.json")
    st.session_state.config = cfg
    st.session_state.store = MappingStore(cfg_path, cfg.paths.metadata_dir)
    st.session_state.library = LibraryManager(cfg.paths.input_dir, st.session_state.store)
    st.session_state.current_file = None
    st.session_state.editor_picks = {}      # source_hex -> manual target_hex (current illustration)
    st.session_state.batch_report = None    # last batch run's summary
    st.session_state.active_nav = "library"  # default landing


_bootstrap()


# --------------------------------------------------------------------------- #
# Navigation table
# --------------------------------------------------------------------------- #
# (key, label, render_callable). Groups defined separately below.
_DESTINATIONS = {
    "library":           ("Library",        tab_library.render),
    "grayscale_editor":  ("Editor",         tab_editor.render),
    "grayscale_global":  ("Global Map",     tab_global_map.render),
    "grayscale_batch":   ("Batch Export",   tab_batch.render),
    "grayscale_settings":("Settings",       tab_settings.render),
    "cmyk_editor":       ("Editor",         tab_cmyk_editor.render),
    "cmyk_global":       ("Global Map",     tab_cmyk_global_map.render),
    "cmyk_palette":      ("Palette",        tab_palette.render),
    "cmyk_export":       ("Print Export",   tab_cmyk_export.render),
    "cmyk_settings":     ("Settings",       tab_cmyk_settings.render),
}

_GROUPS: list[tuple[str, list[str]]] = [
    ("Library",   ["library"]),
    ("Grayscale", ["grayscale_editor", "grayscale_global",
                   "grayscale_batch", "grayscale_settings"]),
    ("CMYK",      ["cmyk_editor", "cmyk_global", "cmyk_palette",
                   "cmyk_export", "cmyk_settings"]),
]


def _render_sidebar() -> str:
    """Render the grouped sidebar nav. Returns the active destination key."""
    with st.sidebar:
        for header, keys in _GROUPS:
            st.markdown(f"**{header}**")
            for key in keys:
                label, _ = _DESTINATIONS[key]
                is_active = st.session_state.active_nav == key
                if st.button(
                    label,
                    key=f"nav_btn_{key}",
                    type="primary" if is_active else "secondary",
                    width="stretch",
                ):
                    st.session_state.active_nav = key
                    st.rerun()
            st.write("")  # group spacer

    return st.session_state.active_nav


_active_key = _render_sidebar()
_active_label, _active_render = _DESTINATIONS[_active_key]

# Page header reflects the active destination so users always see the
# pipeline + section name they're working in (no top tab strip to read).
_group_for_active = next(
    (group for group, keys in _GROUPS if _active_key in keys),
    "",
)
st.subheader(f"{_group_for_active} · {_active_label}" if _group_for_active != "Library" else "Library")
_active_render()
