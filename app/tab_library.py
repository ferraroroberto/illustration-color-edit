"""Library tab — list SVGs in input/ with status badges and quick actions.

Uses ``st.dataframe`` multi-row selection so the user can wipe per-file
configuration (grayscale or CMYK) across many illustrations in one click,
without opening each file. ``Open`` works on a single-row selection.
"""

from __future__ import annotations

import streamlit as st

from common import compact_status_counters, open_in_explorer
from src.library_manager import LibraryManager
from src.mapping_store import MappingStore


def _selected_filenames(entries, df_state) -> list[str]:
    """Return the filenames Streamlit reports as currently selected."""
    sel = getattr(df_state, "selection", None) or (
        df_state.get("selection") if isinstance(df_state, dict) else None
    )
    if not sel:
        return []
    rows = sel.get("rows") if isinstance(sel, dict) else getattr(sel, "rows", None) or []
    return [entries[i].filename for i in rows if 0 <= i < len(entries)]


def render() -> None:
    library: LibraryManager = st.session_state.library
    store: MappingStore = st.session_state.store
    cfg = st.session_state.config

    # ---- Header strip -------------------------------------------------------
    cols = st.columns([4, 1, 1, 1])
    cols[0].markdown(f"**Input directory:** `{cfg.paths.input_dir}`")

    if cols[1].button("📂 Open input folder", key="lib_open_input", width="stretch"):
        ok, msg = open_in_explorer(cfg.paths.input_dir)
        (st.success if ok else st.error)(msg)

    if cols[2].button("Rescan", key="lib_rescan", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    if cols[3].button("Open next pending", key="lib_open_next", width="stretch"):
        nxt = library.next_pending()
        if nxt:
            st.session_state.current_file = nxt.filename
            st.session_state.editor_picks = {}
            st.success(f"Opened {nxt.filename}. Switch to the **Editor** tab.")
        else:
            st.info("No pending illustrations.")

    # Compact one-line counters per pipeline, on their own row so the header
    # strip stays tidy regardless of window width.
    gs = library.status_counts()
    cm = library.cmyk_status_counts()
    st.markdown(
        compact_status_counters("Gray", gs) + compact_status_counters("CMYK", cm),
        unsafe_allow_html=True,
    )

    entries = library.scan()
    if not entries:
        st.warning(f"No SVG files in {cfg.paths.input_dir}.")
        return

    # ---- Dataframe with native multi-row selection --------------------------
    rows = [
        {
            "File": e.filename,
            "Gray": e.status,
            "Gray ovr": e.override_count,
            "CMYK": e.cmyk_status,
            "CMYK ovr": e.cmyk_override_count,
            "Size KB": round(e.size_kb, 1),
            "Modified": e.modified_iso[:19].replace("T", " ") if e.modified_iso else "—",
        }
        for e in entries
    ]
    df_state = st.dataframe(
        rows,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        key="lib_df",
    )
    selected = _selected_filenames(entries, df_state)

    # ---- Action bar (selection-driven) --------------------------------------
    st.markdown(
        f"**Selected:** {len(selected)}"
        + (f" — `{selected[0]}`" + (f" (+{len(selected)-1} more)" if len(selected) > 1 else "")
           if selected else " — pick row(s) above")
    )
    a1, a2, a3, _ = st.columns([1, 2, 2, 4])
    if a1.button(
        "Open",
        key="lib_open_sel",
        type="primary",
        disabled=len(selected) != 1,
        width="stretch",
        help="Open the single selected illustration in the Editor.",
    ):
        st.session_state.current_file = selected[0]
        st.session_state.editor_picks = {}
        st.toast(f"Opened {selected[0]}. Switch to the **Editor** tab.")

    if a2.button(
        f"Wipe grayscale ({len(selected)})",
        key="lib_wipe_gs_sel",
        disabled=len(selected) == 0,
        width="stretch",
        help=(
            "Clear per-file grayscale overrides and reset grayscale status "
            "to 'pending' for the selected files. CMYK config untouched."
        ),
    ):
        n = store.wipe_pipeline(selected, "grayscale")
        st.success(f"Wiped grayscale config on {n} file{'s' if n != 1 else ''}.")
        st.rerun()

    if a3.button(
        f"Wipe CMYK ({len(selected)})",
        key="lib_wipe_cmyk_sel",
        disabled=len(selected) == 0,
        width="stretch",
        help=(
            "Clear per-file CMYK overrides and reset CMYK status to "
            "'pending' for the selected files. Grayscale config untouched."
        ),
    ):
        n = store.wipe_pipeline(selected, "cmyk")
        st.success(f"Wiped CMYK config on {n} file{'s' if n != 1 else ''}.")
        st.rerun()

    # ---- Wipe-ALL (gated by confirm checkbox) -------------------------------
    with st.expander("⚠ Wipe ALL — clear configuration across the whole library", expanded=False):
        st.caption(
            "Resets per-file overrides + status for every illustration in "
            "`metadata/` for the chosen pipeline. The other pipeline is left "
            "alone. Global maps are not touched."
        )
        all_filenames = [e.filename for e in entries]
        confirm = st.checkbox(
            "Yes, I understand this wipes every per-file override for the chosen pipeline.",
            key="lib_wipe_all_confirm",
            value=False,
        )
        wa1, wa2, _ = st.columns([2, 2, 4])
        if wa1.button(
            f"Wipe ALL grayscale ({len(all_filenames)} files)",
            key="lib_wipe_gs_all",
            disabled=not confirm,
            width="stretch",
        ):
            n = store.wipe_pipeline(all_filenames, "grayscale")
            st.success(f"Wiped grayscale config on {n} file{'s' if n != 1 else ''}.")
            st.rerun()
        if wa2.button(
            f"Wipe ALL CMYK ({len(all_filenames)} files)",
            key="lib_wipe_cmyk_all",
            disabled=not confirm,
            width="stretch",
        ):
            n = store.wipe_pipeline(all_filenames, "cmyk")
            st.success(f"Wiped CMYK config on {n} file{'s' if n != 1 else ''}.")
            st.rerun()
