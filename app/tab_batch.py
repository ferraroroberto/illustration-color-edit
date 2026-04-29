"""Batch Export tab — convert reviewed (or all) illustrations and report results."""

from __future__ import annotations

import streamlit as st

from src.library_manager import LibraryManager
from src.mapping_store import MappingStore, merge_mappings
from src.svg_writer import write_converted_svg


def render() -> None:
    st.subheader("Batch export")
    library: LibraryManager = st.session_state.library
    store: MappingStore = st.session_state.store
    cfg = st.session_state.config

    only_reviewed = st.checkbox(
        "Only export reviewed illustrations", value=True, key="batch_reviewed"
    )
    st.markdown(f"**Output directory:** `{cfg.paths.output_dir}`")

    entries = library.scan()
    if only_reviewed:
        entries = [e for e in entries if e.status == "reviewed"]

    st.write(f"{len(entries)} illustration(s) queued.")

    if st.button("Run batch export", type="primary", key="batch_run", width="content"):
        if not entries:
            st.warning("Nothing to export.")
        else:
            cfg.paths.output_dir.mkdir(parents=True, exist_ok=True)
            global_map = store.load_global_map()
            log_rows: list[dict] = []
            progress = st.progress(0.0)
            for i, e in enumerate(entries, start=1):
                illu = store.load_illustration(e.filename)
                merged = merge_mappings(global_map, illu.overrides)
                dst = cfg.paths.output_dir / e.filename
                report = write_converted_svg(e.path, merged, dst)
                illu.with_status("exported")
                store.save_illustration(illu)
                log_rows.append({
                    "file": e.filename,
                    "replacements": report.replacements,
                    "unmapped_colors": len(report.unmapped),
                    "unmapped_list": ", ".join(sorted(report.unmapped)[:8]),
                })
                progress.progress(i / len(entries))
            st.session_state.batch_report = log_rows
            st.success(f"Exported {len(entries)} files to {cfg.paths.output_dir}.")

    rows = st.session_state.get("batch_report")
    if rows:
        st.markdown("### Last run report")
        st.dataframe(rows, width="stretch")
