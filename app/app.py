"""
Streamlit entry point.

Scaffolding only — bootstraps config + session state, then routes the five
horizontal tabs to ``app/tab_*.py``. All real logic lives in ``src/``;
all per-tab UI lives in its own ``tab_*.py`` file.

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


st.set_page_config(layout="wide", page_title="Illustration Color Edit")


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


_bootstrap()


st.title("Illustration Color Edit")
st.caption("SVG → grayscale conversion pipeline for the book project.")

t_lib, t_edit, t_global, t_batch, t_settings = st.tabs(
    ["Library", "Editor", "Global Map", "Batch Export", "Settings"]
)
with t_lib:
    tab_library.render()
with t_edit:
    tab_editor.render()
with t_global:
    tab_global_map.render()
with t_batch:
    tab_batch.render()
with t_settings:
    tab_settings.render()
