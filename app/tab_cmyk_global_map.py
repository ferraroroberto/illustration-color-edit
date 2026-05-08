"""CMYK Global Map tab — view, edit, and add entries to the project-wide
RGB→RGB pre-correction map used by the CMYK pipeline.

Mirrors ``tab_global_map.py``; the only differences are the data source
(``cmyk_correction_map`` instead of ``global_color_map``) and the helper
text framing.
"""

from __future__ import annotations

import streamlit as st

from common import color_swatch
from src.mapping_store import MappingStore


def render() -> None:
    st.subheader("CMYK correction map")
    st.caption(
        "Project-wide RGB→RGB pre-corrections applied before the ICC profile "
        "converts to CMYK. Use this to nudge out-of-gamut colors into a "
        "print-safe RGB starting point. The ICC profile does the actual CMYK "
        "math; these entries just steer it."
    )
    store: MappingStore = st.session_state.store

    gm = store.load_cmyk_correction_map()
    usage = store.cmyk_usage_counts()

    if not gm:
        st.info(
            "CMYK correction map is empty. Add starter corrections in the CMYK "
            "Editor, or seed from `color-config.json.example`."
        )
    else:
        header = st.columns([1, 2, 1, 2, 3, 1])
        for i, label in enumerate(["Source", "Target", "Used in", "Label", "Notes", ""]):
            header[i].markdown(f"**{label}**")

        for src in sorted(gm):
            entry = gm[src]
            row = st.columns([1, 2, 1, 2, 3, 1])
            row[0].markdown(
                f"{color_swatch(src)} <code>{src}</code>",
                unsafe_allow_html=True,
            )
            new_target = row[1].color_picker(
                "target", value=entry["target"], key=f"cmyk_gm_t_{src}",
                label_visibility="collapsed",
            ).upper()
            row[2].write(usage.get(src, 0))
            new_label = row[3].text_input(
                "label", value=entry.get("label", ""), key=f"cmyk_gm_l_{src}",
                label_visibility="collapsed",
            )
            new_notes = row[4].text_input(
                "notes", value=entry.get("notes", ""), key=f"cmyk_gm_n_{src}",
                label_visibility="collapsed",
            )
            if row[5].button("✕", key=f"cmyk_gm_del_{src}", help="Remove entry"):
                store.remove_cmyk_correction_entry(src)
                st.rerun()

            if (
                new_target != entry["target"]
                or new_label != entry.get("label", "")
                or new_notes != entry.get("notes", "")
            ):
                store.upsert_cmyk_correction_entry(
                    src, new_target, label=new_label, notes=new_notes
                )

    st.divider()
    st.markdown("**Add a new entry**")
    with st.form("cmyk_add_global", clear_on_submit=True):
        f = st.columns([1, 1, 2, 3, 1])
        nsrc = f[0].text_input("source hex", value="#")
        ntgt = f[1].color_picker("target", value="#888888")
        nlbl = f[2].text_input("label")
        nnts = f[3].text_input("notes")
        if f[4].form_submit_button("Add"):
            if not nsrc.startswith("#") or len(nsrc) != 7:
                st.error("Source must be #RRGGBB.")
            else:
                store.upsert_cmyk_correction_entry(
                    nsrc.upper(), ntgt.upper(), label=nlbl, notes=nnts
                )
                st.success(f"Added {nsrc.upper()} → {ntgt.upper()}.")
