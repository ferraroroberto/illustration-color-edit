"""Global Map tab — view, edit, and add entries to the project-wide color map."""

from __future__ import annotations

import streamlit as st

from common import render_map_editor
from src.mapping_store import MappingStore


def render() -> None:
    store: MappingStore = st.session_state.store
    render_map_editor(
        store.load_global_map,
        store.usage_counts,
        store.upsert_global_entry,
        store.remove_global_entry,
        key_prefix="gm",
        empty_message="Global map is empty. Map a few colors in the Editor first.",
    )
