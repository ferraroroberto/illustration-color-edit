"""Semantic Palette tab — named slots layered on top of hex mappings.

Lets you bind source hexes (e.g. ``#E74C3C``) to slots (``status.bad``)
and the slots to per-pipeline targets within a theme. Resolution at
render time is: per-file override → semantic theme → legacy global
map → pass-through, so adoption is incremental and additive.

The first time you open this tab on a project that already has global
maps, click **Migrate** to bulk-promote them into auto-named slots
(``auto.001`` …). After that, rename the slots to their semantic
intent and the rest of the pipeline picks up the changes automatically.
"""

from __future__ import annotations

import streamlit as st

from common import color_swatch, load_semantic_palette, semantic_palette_store
from src.mapping_store import MappingStore
from src.semantic_palette import SemanticPalette, auto_migrate_global_map


def _render_migration_panel(store: MappingStore, palette: SemanticPalette) -> None:
    with st.expander(
        "Migrate existing global maps to slots",
        expanded=not palette.slots,
    ):
        st.caption(
            "Walks `global_color_map` and `cmyk_correction_map`, creating "
            "an auto-named slot for every entry that isn't already claimed "
            "by an existing slot. Idempotent — safe to re-run."
        )
        c1, c2, c3 = st.columns(3)
        if c1.button(
            "Migrate grayscale global map",
            key="sem_migrate_gs",
            width="stretch",
        ):
            n = auto_migrate_global_map(
                palette,
                store.load_global_map(),
                "grayscale",
            )
            semantic_palette_store().save(palette)
            st.success(
                f"Created {n} slot{'s' if n != 1 else ''} from "
                "`global_color_map`."
            )
            st.rerun()
        if c2.button(
            "Migrate CMYK correction map",
            key="sem_migrate_cmyk",
            width="stretch",
        ):
            n = auto_migrate_global_map(
                palette,
                store.load_cmyk_correction_map(),
                "cmyk",
            )
            semantic_palette_store().save(palette)
            st.success(
                f"Created {n} slot{'s' if n != 1 else ''} from "
                "`cmyk_correction_map`."
            )
            st.rerun()
        if c3.button(
            "Migrate both pipelines",
            key="sem_migrate_both",
            type="primary",
            width="stretch",
        ):
            n_gs = auto_migrate_global_map(
                palette, store.load_global_map(), "grayscale",
            )
            n_cmyk = auto_migrate_global_map(
                palette, store.load_cmyk_correction_map(), "cmyk",
            )
            semantic_palette_store().save(palette)
            st.success(
                f"Created {n_gs} grayscale slot{'s' if n_gs != 1 else ''} "
                f"and {n_cmyk} CMYK slot{'s' if n_cmyk != 1 else ''}."
            )
            st.rerun()


def _render_active_theme_picker(palette: SemanticPalette) -> None:
    options = sorted(set(palette.themes.keys()) | {palette.active_theme, "default"})
    cols = st.columns([3, 2, 2])
    with cols[0]:
        chosen = st.selectbox(
            "Active theme",
            options=options,
            index=options.index(palette.active_theme),
            key="sem_active_theme",
        )
        if chosen != palette.active_theme:
            palette.active_theme = chosen
            semantic_palette_store().save(palette)
            st.rerun()
    with cols[1]:
        new_name = st.text_input(
            "Add theme",
            placeholder="e.g. acme-uncoated",
            key="sem_new_theme",
        )
    with cols[2]:
        st.write("")  # baseline
        st.write("")
        if st.button("Add", key="sem_add_theme", width="stretch"):
            cleaned = new_name.strip()
            if not cleaned:
                st.error("Pick a name.")
            elif cleaned in palette.themes:
                st.error(f"Theme {cleaned!r} already exists.")
            else:
                palette.themes[cleaned] = palette.themes.get(palette.active_theme).__class__()
                palette.active_theme = cleaned
                semantic_palette_store().save(palette)
                st.success(f"Created theme {cleaned!r}.")
                st.rerun()


def _render_add_slot_form(palette: SemanticPalette) -> None:
    with st.expander("Add a new slot", expanded=not palette.slots):
        c1, c2, c3, c4 = st.columns([2, 2, 3, 1])
        name = c1.text_input(
            "Slot name", placeholder="status.bad", key="sem_new_slot_name",
        )
        authored = c2.color_picker(
            "Authored hex", value="#E74C3C", key="sem_new_slot_hex",
        )
        label = c3.text_input(
            "Human label", placeholder="Bad / negative",
            key="sem_new_slot_label",
        )
        c4.write("")
        c4.write("")
        if c4.button("Add", key="sem_new_slot_add", type="primary", width="stretch"):
            cleaned = name.strip()
            if not cleaned:
                st.error("Slot name is required.")
            elif cleaned in palette.slots:
                st.error(f"Slot {cleaned!r} already exists.")
            else:
                palette.upsert_slot(
                    name=cleaned,
                    authored=authored.upper(),
                    label=label,
                )
                semantic_palette_store().save(palette)
                st.rerun()


def _render_slot_table(palette: SemanticPalette) -> None:
    if not palette.slots:
        st.info(
            "No slots yet. Use **Add a new slot** above or **Migrate** "
            "to bulk-promote existing global-map entries."
        )
        return
    theme = palette.active()

    st.markdown(f"##### Slots in theme `{palette.active_theme}`")
    for name, slot in sorted(palette.slots.items()):
        with st.container(border=True):
            row = st.columns([3, 2, 2, 2, 1])
            row[0].markdown(
                f"{color_swatch(slot.authored)} **`{name}`**  "
                f"<small>(authored {slot.authored})</small>",
                unsafe_allow_html=True,
            )
            new_label = row[0].text_input(
                "label",
                value=slot.label,
                key=f"sem_lbl_{name}",
                label_visibility="collapsed",
                placeholder="Human label",
            )
            new_authored = row[1].color_picker(
                "Authored",
                value=slot.authored,
                key=f"sem_authored_{name}",
            ).upper()

            cmyk_existing = theme.cmyk.get(name, "#000000")
            new_cmyk = row[2].color_picker(
                "CMYK target",
                value=cmyk_existing,
                key=f"sem_cmyk_{name}",
            ).upper()
            cmyk_active = name in theme.cmyk
            row[2].caption(
                "active" if cmyk_active else "(not set in this theme)"
            )

            gs_existing = theme.grayscale.get(name, "#888888")
            new_gs = row[3].color_picker(
                "grayscale target",
                value=gs_existing,
                key=f"sem_gs_{name}",
            ).upper()
            gs_active = name in theme.grayscale
            row[3].caption(
                "active" if gs_active else "(not set in this theme)"
            )

            row[4].write("")
            row[4].write("")
            if row[4].button(
                "Delete",
                key=f"sem_del_{name}",
                width="stretch",
                help="Remove the slot and unbind it from all themes.",
            ):
                palette.remove_slot(name)
                semantic_palette_store().save(palette)
                st.rerun()

            dirty = False
            if new_label != slot.label:
                slot.label = new_label
                dirty = True
            if new_authored != slot.authored:
                slot.authored = new_authored
                dirty = True
            if cmyk_active and new_cmyk != cmyk_existing:
                palette.set_theme_target(name, "cmyk", new_cmyk)
                dirty = True
            if gs_active and new_gs != gs_existing:
                palette.set_theme_target(name, "grayscale", new_gs)
                dirty = True

            br = st.columns(4)
            if not cmyk_active and br[0].button(
                "Bind CMYK target", key=f"sem_bind_cmyk_{name}",
            ):
                palette.set_theme_target(name, "cmyk", new_cmyk)
                dirty = True
            if cmyk_active and br[1].button(
                "Clear CMYK", key=f"sem_clear_cmyk_{name}",
            ):
                palette.clear_theme_target(name, "cmyk")
                dirty = True
            if not gs_active and br[2].button(
                "Bind grayscale target", key=f"sem_bind_gs_{name}",
            ):
                palette.set_theme_target(name, "grayscale", new_gs)
                dirty = True
            if gs_active and br[3].button(
                "Clear grayscale", key=f"sem_clear_gs_{name}",
            ):
                palette.clear_theme_target(name, "grayscale")
                dirty = True

            if dirty:
                semantic_palette_store().save(palette)
                st.rerun()


def render() -> None:
    store: MappingStore = st.session_state.store
    palette = load_semantic_palette()

    st.markdown(
        "Slots decouple authored colors (the hex you painted with) from "
        "their per-pipeline targets (what the press sees). Edits here "
        "propagate to every illustration that uses an authored color, "
        "without touching per-file overrides."
    )
    _render_active_theme_picker(palette)
    st.divider()
    _render_migration_panel(store, palette)
    _render_add_slot_form(palette)
    _render_slot_table(palette)
