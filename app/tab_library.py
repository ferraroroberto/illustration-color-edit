"""Library tab — list SVGs in input/ with status badges and quick actions."""

from __future__ import annotations

import streamlit as st

from common import status_badge
from src.library_manager import LibraryManager


def render() -> None:
    st.subheader("Library")
    library: LibraryManager = st.session_state.library
    cfg = st.session_state.config

    cols = st.columns([3, 1, 1, 1])
    cols[0].markdown(f"**Input directory:** `{cfg.paths.input_dir}`")

    if cols[1].button("Rescan", key="lib_rescan", width="content"):
        st.cache_data.clear()
        st.rerun()

    if cols[2].button("Open next pending", key="lib_open_next", width="content"):
        nxt = library.next_pending()
        if nxt:
            st.session_state.current_file = nxt.filename
            st.session_state.editor_picks = {}
            st.success(f"Opened {nxt.filename}. Switch to the **Editor** tab.")
        else:
            st.info("No pending illustrations.")

    counts = library.status_counts()
    cols[3].markdown(
        f"<div style='line-height:1.6'>"
        f"{status_badge('pending')} {counts['pending']} &nbsp;"
        f"{status_badge('in_progress')} {counts['in_progress']} &nbsp;"
        f"{status_badge('reviewed')} {counts['reviewed']} &nbsp;"
        f"{status_badge('exported')} {counts['exported']}"
        f"</div>",
        unsafe_allow_html=True,
    )

    entries = library.scan()
    if not entries:
        st.warning(f"No SVG files in {cfg.paths.input_dir}.")
        return

    header = st.columns([3, 1, 1, 1, 1, 1])
    for i, label in enumerate(["File", "Status", "Overrides", "Size (KB)", "Modified", "Open"]):
        header[i].markdown(f"**{label}**")

    for e in entries:
        row = st.columns([3, 1, 1, 1, 1, 1])
        row[0].write(e.filename)
        row[1].markdown(status_badge(e.status), unsafe_allow_html=True)
        row[2].write(e.override_count)
        row[3].write(f"{e.size_kb:.1f}")
        row[4].write(e.modified_iso[:19].replace("T", " ") if e.modified_iso else "—")
        if row[5].button("Open", key=f"open_{e.filename}", width="content"):
            st.session_state.current_file = e.filename
            st.session_state.editor_picks = {}
            st.toast(f"Opened {e.filename}. Switch to the **Editor** tab.")
