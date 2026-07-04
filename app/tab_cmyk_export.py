"""CMYK Print Export tab — batch convert SVGs to CMYK PDFs.

Mirrors ``tab_batch.py`` in shape: filter (only-reviewed), run button,
progress bar, per-file status table. Settings (ICC profile, dimensions,
PDF/X mode, etc.) live in the dedicated **CMYK → Settings** tab; this
tab only reads them.
"""

from __future__ import annotations

import streamlit as st

from common import load_semantic_palette, open_in_explorer
from src.cmyk_pipeline import (
    FileResult,
    build_batch_plan_factory,
    build_cmyk_context,
    process_batch,
)
from src.cmyk_convert import pdfx_mode_label
from src.config import PROJECT_ROOT, CmykExportConfig
from tab_cmyk_settings import persist_settings as _persist_cmyk_settings
from src.delivery import create_snapshot
from src.library_manager import LibraryManager
from src.mapping_store import MappingStore
from src.qa_report import write_report


def render() -> None:
    library: LibraryManager = st.session_state.library
    store: MappingStore = st.session_state.store
    cfg = st.session_state.config
    ce: CmykExportConfig = cfg.cmyk_export

    # ---- Active settings summary (read-only) ------------------------------ #
    pdfx_label = pdfx_mode_label(ce.pdfx_compliance)
    trim_line = (
        f"**Trim to content:** ON (+{ce.trim_to_content_padding_pt:g}pt padding) — "
        "page = artwork extent; configured trim/bleed bypassed"
        if ce.trim_to_content_enabled
        else "**Trim to content:** OFF — using configured trim/bleed below"
    )
    full_preview_line = (
        f"**Full preview:** {'ON' if ce.generate_full_preview else 'OFF'}"
    )
    st.info(
        f"{trim_line}  \n"
        f"**ICC:** `{ce.icc_profile_path.name}` · "
        f"**Trim:** {ce.target_width_inches:.3f} × {ce.target_height_inches:.3f} in · "
        f"**Bleed:** {ce.bleed_inches:.3f} in · "
        f"**Spec:** {pdfx_label}  \n"
        f"**Print folder:** `{ce.print_dir}`  \n"
        f"**Preview folder:** `{ce.preview_dir}` · {full_preview_line}  \n"
        "Edit these in **CMYK → Settings**."
    )

    # ---- Trim-to-content (inline override) -------------------------------- #
    # Exposed inline because flipping this changes what the publisher gets
    # at the page level — easier to reach from the same tab that runs the
    # batch than to bounce through Settings. Persisted to config.json so
    # the next batch run (and the CLI) see the same state.
    with st.expander(
        f"Trim PDF to content bounds — {'ON' if ce.trim_to_content_enabled else 'OFF'}",
        expanded=ce.trim_to_content_enabled,
    ):
        st.caption(
            "When on, the PDF page size matches the artwork's actual extent "
            "(replaces the configured trim and bleed). Soft-proof guides are "
            "suppressed because there are no trim/bleed/safety margins to draw."
        )
        t1, t2 = st.columns([1, 2])
        ce.trim_to_content_enabled = t1.checkbox(
            "Trim PDF to content bounds",
            value=ce.trim_to_content_enabled,
            key="cmyk_export_trim_enabled",
        )
        ce.trim_to_content_padding_pt = t2.number_input(
            "Padding around content (pt)",
            min_value=0.0, max_value=20.0,
            value=float(ce.trim_to_content_padding_pt), step=0.5,
            key="cmyk_export_trim_padding",
            help="Pt = PostScript points (1 pt = 1/72 in). 0 = bbox flush.",
        )
        # Always render the save button — gating it on a dirty flag races
        # with Streamlit's widget→state sync (by the time the click fires
        # the next rerun, ``ce`` already matches the widget so dirty=False
        # and the button vanishes without ever executing).
        if st.button("Save trim setting to config.json",
                     key="cmyk_export_save_trim", width="content"):
            written = _persist_cmyk_settings(cfg)
            if written:
                st.success(f"Saved to `{written}`")
            else:
                st.error("No config.json found to save into.")

    # ---- Pre-flight checks ------------------------------------------------- #
    if not ce.icc_profile_path.is_file():
        st.warning(
            f"ICC profile not found at `{ce.icc_profile_path}`. Conversion will "
            "fail until you place a profile there. See the README for sources."
        )

    open_col, _ = st.columns([1, 5])
    if open_col.button("📂 Open output folder", key="cmyk_export_open_out",
                       width="content"):
        ok, msg = open_in_explorer(ce.output_dir)
        (st.success if ok else st.error)(msg)

    only_reviewed = st.checkbox(
        "Only export CMYK-reviewed illustrations", value=False,
        key="cmyk_batch_reviewed",
        help="Reviewed in the CMYK Editor (independent of grayscale review).",
    )

    entries = library.scan()
    if only_reviewed:
        entries = [e for e in entries if store.load_illustration(e.filename).cmyk_status == "reviewed"]
    st.write(f"{len(entries)} illustration(s) queued.")
    if ce.trim_to_content_enabled:
        page_line = (
            f"**Page:** trim to content (+{ce.trim_to_content_padding_pt:g}pt) · "
            f"**Bleed:** 0 in (overridden)"
        )
    else:
        page_line = (
            f"**Trim:** {ce.target_width_inches:.3f} × {ce.target_height_inches:.3f} in · "
            f"**Bleed:** {ce.bleed_inches:.3f} in"
        )
    st.markdown(
        f"**Output:** `{ce.output_dir}` · "
        f"{page_line} · "
            f"**PDF/X:** {pdfx_label}"
    )

    # ---- Run --------------------------------------------------------------- #
    if st.button("Run CMYK batch export", type="primary",
                 key="cmyk_batch_run", width="content"):
        if not entries:
            st.warning("Nothing to export.")
            return

        ctx = build_cmyk_context(cfg)

        # Build per-file mapping list. Each illustration gets its own merge
        # of (global cmyk_correction_map + per-file cmyk_overrides).
        cmyk_global = store.load_cmyk_correction_map()
        cmyk_device_global = store.load_cmyk_device_overrides()
        progress = st.progress(0.0)
        status_box = st.empty()

        sem = load_semantic_palette()
        plan_file = build_batch_plan_factory(store, cmyk_global, cmyk_device_global, ctx, sem)

        def _on_progress(i: int, total: int, r: FileResult) -> None:
            status_box.markdown(
                f"`{i}/{total}` **{r.filename}** — "
                f"{r.status} ({r.elapsed_seconds:.2f}s)"
            )
            progress.progress(i / total)

        report = process_batch(
            [e.path for e in entries],
            {},  # per-file correction maps come from plan_file
            ctx,
            on_progress=_on_progress,
            plan_file=plan_file,
            # palette_mapped reflects the global correction map — per-file
            # overrides get reported per-file via FileResult.replacements.
            palette_mapped={k: v["target"] for k, v in cmyk_global.items()},
        )

        qa_path = write_report(report, ce.print_dir)
        st.session_state["cmyk_batch_report"] = {
            "files": [
                {
                    "file": r.filename,
                    "status": r.status,
                    "replacements": r.replacements,
                    "unmapped": len(r.unmapped_colors),
                    "warnings": len(r.warnings),
                    "elapsed_s": r.elapsed_seconds,
                    "pdf": str(r.output_pdf) if r.output_pdf else "",
                    "preview": str(r.preview_png) if r.preview_png else "",
                    "error": r.error or "",
                    # Trim-to-content columns. Empty strings when trim was
                    # off or the SVG had no visible content (fallback path).
                    "original_viewBox": (
                        r.trim.original_viewbox if r.trim and r.trim.had_content else ""
                    ),
                    "trimmed_viewBox": (
                        r.trim.new_viewbox if r.trim and r.trim.had_content else ""
                    ),
                    "page_inches": (
                        f"{r.trim.width_in:.3f} × {r.trim.height_in:.3f}"
                        if r.trim and r.trim.had_content
                        else ""
                    ),
                }
                for r in report.files
            ],
            "qa_path": str(qa_path),
            "total_s": report.total_seconds,
            "succeeded": report.succeeded,
            "failed": report.failed,
        }
        if report.failed == 0:
            st.success(
                f"Exported {report.succeeded} files in {report.total_seconds:.2f}s. "
                f"QA report: `{qa_path}`"
            )
        else:
            st.warning(
                f"{report.succeeded} ok, {report.failed} failed. See QA report at `{qa_path}`."
            )

    # ---- Last-run report --------------------------------------------------- #
    last = st.session_state.get("cmyk_batch_report")
    if last:
        st.divider()
        st.markdown("### Last run")
        st.markdown(
            f"`{last['succeeded']} ok` · `{last['failed']} failed` · "
            f"`{last['total_s']:.2f}s` · "
            f"[QA report]({last['qa_path']})"
        )
        st.dataframe(last["files"], width="stretch")

    # ---- Delivery snapshot ------------------------------------------------- #
    st.divider()
    st.markdown("### Create delivery package")
    st.caption(
        "Freezes the current `config.json`, `color-config.json`, and "
        "`semantic-palette.json` alongside hardlinked copies of every PDF "
        "in the output directory. Use one snapshot per publisher hand-off "
        "so tweaks weeks later are byte-reproducible."
    )
    dc1, dc2, dc3 = st.columns([3, 2, 2])
    label = dc1.text_input(
        "Delivery label",
        placeholder="acme-2026-05",
        key="cmyk_delivery_label",
    )
    pattern = dc2.text_input(
        "PDF glob pattern",
        value="*_CMYK.pdf",
        key="cmyk_delivery_pattern",
        help="Change to '*.pdf' if you set a custom filename template.",
    )
    dc3.write("")
    dc3.write("")
    if dc3.button(
        "Create snapshot", key="cmyk_delivery_btn",
        type="primary", width="stretch",
    ):
        if not label.strip():
            st.error("Pick a label first.")
        else:
            try:
                target = create_snapshot(
                    label=label,
                    project_root=PROJECT_ROOT,
                    output_dir=ce.print_dir,
                    preview_dir=ce.preview_dir,
                    pdf_pattern=pattern,
                    icc_profile=str(ce.icc_profile_path),
                    pdfx=ce.pdfx_compliance,
                    width_inches=ce.target_width_inches,
                    height_inches=ce.target_height_inches,
                    bleed_inches=ce.bleed_inches,
                )
                st.success(f"Snapshot written to `{target}`")
                ok, msg = open_in_explorer(target)
                if not ok:
                    st.caption(msg)
            except Exception as exc:
                st.error(f"Snapshot failed: {exc}")
