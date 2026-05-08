"""CMYK Print Export tab — batch convert SVGs to CMYK PDFs.

Mirrors ``tab_batch.py`` in shape: filter (only-reviewed), run button,
progress bar, per-file status table. Settings (ICC profile, dimensions,
PDF/X mode, etc.) live in the dedicated **CMYK → Settings** tab; this
tab only reads them.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from common import open_in_explorer
from src.cmyk_pipeline import CmykContext, process_batch
from src.config import CmykExportConfig
from src.library_manager import LibraryManager
from src.mapping_store import MappingStore, merge_mappings
from src.qa_report import write_report


def render() -> None:
    library: LibraryManager = st.session_state.library
    store: MappingStore = st.session_state.store
    cfg = st.session_state.config
    ce: CmykExportConfig = cfg.cmyk_export

    # ---- Active settings summary (read-only) ------------------------------ #
    pdfx_label = "PDF/X-1a:2003" if ce.pdfx_compliance else "plain DeviceCMYK"
    st.info(
        f"**ICC:** `{ce.icc_profile_path.name}` · "
        f"**Trim:** {ce.target_width_inches:.3f} × {ce.target_height_inches:.3f} in · "
        f"**Bleed:** {ce.bleed_inches:.3f} in · "
        f"**Spec:** {pdfx_label} · "
        f"**Output:** `{ce.output_dir}`  \n"
        "Edit these in **CMYK → Settings**."
    )

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
    st.markdown(
        f"**Output:** `{ce.output_dir}` · "
        f"**Trim:** {ce.target_width_inches:.3f} × {ce.target_height_inches:.3f} in · "
        f"**Bleed:** {ce.bleed_inches:.3f} in · "
        f"**PDF/X:** {'on' if ce.pdfx_compliance else 'off'}"
    )

    # ---- Run --------------------------------------------------------------- #
    if st.button("Run CMYK batch export", type="primary",
                 key="cmyk_batch_run", width="content"):
        if not entries:
            st.warning("Nothing to export.")
            return

        ctx = CmykContext(
            output_dir=ce.output_dir,
            icc_profile=ce.icc_profile_path,
            inkscape_exe=cfg.png_export.inkscape_path,
            ghostscript_exe=ce.ghostscript_path,
            width_inches=ce.target_width_inches,
            height_inches=ce.target_height_inches,
            bleed_inches=ce.bleed_inches,
            pdfx=ce.pdfx_compliance,
            generate_preview=ce.generate_preview_png,
            preview_dpi=ce.preview_dpi,
            audit_artifacts=ce.audit_artifacts,
        )

        # Build per-file mapping list. Each illustration gets its own merge
        # of (global cmyk_correction_map + per-file cmyk_overrides).
        cmyk_global = store.load_cmyk_correction_map()
        progress = st.progress(0.0)
        status_box = st.empty()

        from src.cmyk_pipeline import BatchReport, process_one
        import time as _time
        from datetime import datetime, timezone

        report = BatchReport(
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            icc_profile=str(ce.icc_profile_path),
            pdfx=ce.pdfx_compliance,
            width_inches=ce.target_width_inches,
            height_inches=ce.target_height_inches,
            bleed_inches=ce.bleed_inches,
        )
        # Build palette across queued files (best-effort).
        from src.svg_parser import parse_svg
        palette: dict[str, int] = {}
        for e in entries:
            try:
                for h in parse_svg(e.path).colors:
                    palette[h] = palette.get(h, 0) + 1
            except Exception:
                pass
        report.palette = palette
        # palette_mapped reflects the global correction map — per-file
        # overrides get reported per-file via FileResult.replacements.
        report.palette_mapped = {k: v["target"] for k, v in cmyk_global.items()}

        t_start = _time.time()
        for i, e in enumerate(entries, start=1):
            illu = store.load_illustration(e.filename)
            full_mapping = merge_mappings(cmyk_global, illu.cmyk_overrides)
            r = process_one(e.path, full_mapping, ctx)
            report.files.append(r)
            if r.status == "ok":
                illu.with_cmyk_status("exported")
                store.save_illustration(illu)
            status_box.markdown(
                f"`{i}/{len(entries)}` **{e.filename}** — "
                f"{r.status} ({r.elapsed_seconds:.2f}s)"
            )
            progress.progress(i / len(entries))
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        report.total_seconds = round(_time.time() - t_start, 3)

        qa_path = write_report(report, ce.output_dir)
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
