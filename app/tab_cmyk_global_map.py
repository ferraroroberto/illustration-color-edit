"""CMYK Global Map tab — view, edit, and add entries to the project-wide
RGB→RGB pre-correction map used by the CMYK pipeline.

Shares the table + add-form editor with ``tab_global_map.py`` via
:func:`common.render_map_editor`; the only differences are the bound store
methods (``cmyk_correction_map`` instead of ``global_color_map``), the widget
key prefix, and the helper text framing.
"""

from __future__ import annotations

import streamlit as st

from common import render_map_editor
from src.mapping_store import MappingStore


def render() -> None:
    store: MappingStore = st.session_state.store
    render_map_editor(
        store.load_cmyk_correction_map,
        store.cmyk_usage_counts,
        store.upsert_cmyk_correction_entry,
        store.remove_cmyk_correction_entry,
        key_prefix="cmyk_gm",
        caption=(
            "Project-wide RGB→RGB pre-corrections applied before the ICC profile "
            "converts to CMYK. Use this to nudge out-of-gamut colors into a "
            "print-safe RGB starting point. The ICC profile does the actual CMYK "
            "math; these entries just steer it."
        ),
        empty_message=(
            "CMYK correction map is empty. Add starter corrections in the CMYK "
            "Editor, or seed from `color-config.json.example`."
        ),
    )
