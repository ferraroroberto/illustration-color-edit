"""CMYK Settings tab.

Two responsibilities:

1. Show the **active CMYK configuration** at a glance — which ICC profile
   is being used, where it lives on disk, its size, the resolved
   Ghostscript binary, the trim+bleed dimensions, and the PDF/X-1a state.
   The user explicitly asked to see "what mapping and encoding I'm using"
   so the soft-proof is unambiguous.

2. Provide an editable form to change those values and persist them back
   to ``config.json``. Extracted from the in-line panel that used to live
   in ``tab_cmyk_export.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from src.config import CmykExportConfig
from src.filename_template import TemplateError, apply_template
from src.library_manager import LibraryManager


def _persist_settings(cfg) -> Path | None:
    """Write current cmyk_export settings back to config.json in place.

    Returns the path written, or None if no source config file is known.
    """
    cfg_path = cfg.source_path or (Path(__file__).resolve().parent.parent / "config.json")
    if not cfg_path.is_file():
        return None
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw["cmyk_export"] = {
        "enabled": cfg.cmyk_export.enabled,
        "output_dir": str(cfg.cmyk_export.output_dir),
        "icc_profile_path": str(cfg.cmyk_export.icc_profile_path),
        "ghostscript_path": cfg.cmyk_export.ghostscript_path,
        "target_width_inches": cfg.cmyk_export.target_width_inches,
        "target_height_inches": cfg.cmyk_export.target_height_inches,
        "bleed_inches": cfg.cmyk_export.bleed_inches,
        "pdfx_compliance": cfg.cmyk_export.pdfx_compliance,
        "generate_preview_png": cfg.cmyk_export.generate_preview_png,
        "preview_dpi": cfg.cmyk_export.preview_dpi,
        "audit_artifacts": cfg.cmyk_export.audit_artifacts,
        "filename_template": cfg.cmyk_export.filename_template,
        "tac_limit_percent": cfg.cmyk_export.tac_limit_percent,
        "tac_check_dpi": cfg.cmyk_export.tac_check_dpi,
        "force_k_min_stroke_pt": cfg.cmyk_export.force_k_min_stroke_pt,
        "force_k_min_text_pt": cfg.cmyk_export.force_k_min_text_pt,
        "safety_inches": cfg.cmyk_export.safety_inches,
        "show_guide_overlay": cfg.cmyk_export.show_guide_overlay,
    }
    cfg_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return cfg_path


def _render_filename_preview(template: str, library: LibraryManager) -> None:
    """Show what the active template would produce against the first 3 SVGs.

    Pure UI sugar — keeps the user from saving a typoed template and only
    discovering the problem at batch time. Empty template → silent (the
    default behavior is already obvious).
    """
    if not template or not template.strip():
        return
    sample_paths = library.list_svg_paths()[:3]
    if not sample_paths:
        st.caption("Template preview unavailable — `input/` is empty.")
        return
    rows: list[str] = []
    any_error = False
    for path in sample_paths:
        stem = path.stem
        try:
            out_stem = apply_template(template, stem)
            rows.append(f"`{path.name}` → `{out_stem}.pdf`")
        except TemplateError as exc:
            any_error = True
            rows.append(f"`{path.name}` → ⚠ {exc} (falls back to `{stem}_CMYK.pdf`)")
    st.markdown("**Template preview**")
    for r in rows:
        st.markdown(f"- {r}")
    if any_error:
        st.caption(
            "Files without a parseable chapter.figure prefix fall back to "
            "the default `<stem>_CMYK.pdf` and emit a warning per file."
        )


def _format_bytes(n: int) -> str:
    if n >= 1_048_576:
        return f"{n/1_048_576:.2f} MB"
    if n >= 1024:
        return f"{n/1024:.1f} KB"
    return f"{n} B"


def render() -> None:
    cfg = st.session_state.config
    ce: CmykExportConfig = cfg.cmyk_export

    # ---- Active configuration (read-only summary) -------------------------- #
    st.markdown("### Active configuration")
    st.caption("This is what every CMYK soft-proof and batch run uses right now.")

    icc = ce.icc_profile_path
    icc_exists = icc.is_file()
    icc_size = _format_bytes(icc.stat().st_size) if icc_exists else "—"
    pdfx_label = "PDF/X-1a:2003" if ce.pdfx_compliance else "plain DeviceCMYK"

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**ICC profile**")
        if icc_exists:
            st.success(f"`{icc.name}` · {icc_size}")
        else:
            st.error(f"NOT FOUND at `{icc}` — conversions will fail.")
        st.caption(f"Path: `{icc}`")

        st.markdown("**Ghostscript binary**")
        gs = ce.ghostscript_path
        gs_exists = Path(gs).is_file() if gs else False
        if gs_exists:
            st.success(f"`{Path(gs).name}`")
        else:
            st.info(f"`{gs}` — relying on PATH lookup")
        st.caption(f"Path: `{gs}`")

    with c2:
        st.markdown("**Page size**")
        page_w = ce.target_width_inches + 2 * ce.bleed_inches
        page_h = ce.target_height_inches + 2 * ce.bleed_inches
        st.markdown(
            f"Trim: **{ce.target_width_inches:.3f} × {ce.target_height_inches:.3f}** in  \n"
            f"Bleed: **{ce.bleed_inches:.3f}** in (each side)  \n"
            f"PDF MediaBox: **{page_w:.3f} × {page_h:.3f}** in"
        )

        st.markdown("**Output mode**")
        st.markdown(
            f"Spec: **{pdfx_label}**  \n"
            f"Soft-proof PNG: **{'on' if ce.generate_preview_png else 'off'}** "
            f"@ {ce.preview_dpi} dpi  \n"
            f"Audit sidecars: **{'full suite' if ce.audit_artifacts else 'PDF only'}**"
        )

        st.markdown("**Output directory**")
        st.code(str(ce.output_dir), language=None)

    st.divider()

    # ---- Editable form ----------------------------------------------------- #
    st.markdown("### Edit settings")
    st.caption(
        "Changes apply to this session immediately. Click **Save settings to "
        "config.json** to persist."
    )

    c1, c2, c3 = st.columns(3)
    ce.target_width_inches = c1.number_input(
        "Width (inches)", min_value=0.5, max_value=30.0,
        value=float(ce.target_width_inches), step=0.125,
        key="cmyk_settings_w",
    )
    ce.target_height_inches = c2.number_input(
        "Height (inches)", min_value=0.5, max_value=30.0,
        value=float(ce.target_height_inches), step=0.125,
        key="cmyk_settings_h",
    )
    ce.bleed_inches = c3.number_input(
        "Bleed (inches)", min_value=0.0, max_value=1.0,
        value=float(ce.bleed_inches), step=0.0625,
        key="cmyk_settings_bleed",
    )
    ce.icc_profile_path = Path(st.text_input(
        "ICC profile path", value=str(ce.icc_profile_path),
        key="cmyk_settings_icc",
    ))
    ce.ghostscript_path = st.text_input(
        "Ghostscript binary (path or name on PATH)",
        value=ce.ghostscript_path, key="cmyk_settings_gs",
    )
    ce.output_dir = Path(st.text_input(
        "Output directory", value=str(ce.output_dir),
        key="cmyk_settings_outdir",
    ))
    d1, d2, d3 = st.columns(3)
    ce.pdfx_compliance = d1.checkbox(
        "PDF/X-1a:2003", value=ce.pdfx_compliance, key="cmyk_settings_pdfx",
        help="Stricter publisher-friendly PDF spec; forbids transparency.",
    )
    ce.generate_preview_png = d2.checkbox(
        "Generate soft-proof PNGs", value=ce.generate_preview_png,
        key="cmyk_settings_preview",
    )
    ce.preview_dpi = d3.number_input(
        "Preview DPI", min_value=72, max_value=600,
        value=int(ce.preview_dpi), step=24, key="cmyk_settings_dpi",
    )
    ce.audit_artifacts = st.checkbox(
        "Write audit sidecars (per-file report + retain PDF/X def file)",
        value=ce.audit_artifacts, key="cmyk_settings_audit",
        help=(
            "When on, each export drops a `<name>_CMYK_report.txt` next to "
            "the PDF describing the ICC profile, page geometry, color "
            "replacements, and the exact Ghostscript command used — handy "
            "for the book editor or prepress operator. In PDF/X mode the "
            "`.pdfx_def.ps` file is also kept. Turn off for a clean output "
            "folder containing only the final PDFs (and preview PNGs)."
        ),
    )

    st.markdown("##### Print quality gates")
    q1, q2, q3, q4 = st.columns(4)
    ce.tac_limit_percent = q1.number_input(
        "TAC limit (%)", min_value=180.0, max_value=400.0,
        value=float(ce.tac_limit_percent), step=10.0,
        key="cmyk_settings_tac_limit",
        help="Total Area Coverage cap. 320 typical for coated, 240–280 for uncoated.",
    )
    ce.tac_check_dpi = q2.number_input(
        "TAC sample DPI", min_value=72, max_value=300,
        value=int(ce.tac_check_dpi), step=24,
        key="cmyk_settings_tac_dpi",
        help="100 dpi is enough for flat-color art; raise for very fine features.",
    )
    ce.force_k_min_stroke_pt = q3.number_input(
        "Min stroke (pt)", min_value=0.0, max_value=4.0,
        value=float(ce.force_k_min_stroke_pt), step=0.05,
        key="cmyk_settings_min_stroke_pt",
        help="Near-black strokes thinner than this are flagged for force-K.",
    )
    ce.force_k_min_text_pt = q4.number_input(
        "Min text (pt)", min_value=0.0, max_value=24.0,
        value=float(ce.force_k_min_text_pt), step=0.5,
        key="cmyk_settings_min_text_pt",
        help="Near-black text smaller than this is flagged for force-K.",
    )

    st.markdown("##### Soft-proof guides")
    g1, g2 = st.columns([1, 3])
    ce.show_guide_overlay = g1.checkbox(
        "Draw trim/bleed/safety guides on soft-proof PNGs",
        value=ce.show_guide_overlay,
        key="cmyk_settings_show_guides",
        help=(
            "Composites three rectangles on every soft-proof PNG: solid red "
            "trim line, dashed magenta bleed (when bleed > 0), dashed cyan "
            "safety inset. Catches annotations creeping into the cut zone."
        ),
    )
    ce.safety_inches = g2.number_input(
        "Safety margin (inches)", min_value=0.0, max_value=1.0,
        value=float(ce.safety_inches), step=0.0625,
        key="cmyk_settings_safety",
        help="Distance from trim that critical content should stay inside. 0.1875\" ≈ 4.76 mm.",
    )

    ce.filename_template = st.text_input(
        "Output filename template",
        value=ce.filename_template,
        key="cmyk_settings_filename_template",
        help=(
            "Optional template for output PDF stems. Empty = `<stem>_CMYK.pdf` "
            "default. Placeholders: `{stem}`, `{chapter}` (or `{chapter:02d}`), "
            "`{figure}` (or `{figure:02d}`), `{description}`, `{slug}`. "
            "Chapter/figure are auto-parsed from leading numeric prefixes "
            "like `04.03 - …`, `1.2 …`, `4-3 …`, `4_3 …`."
        ),
        placeholder="fig_{chapter:02d}_{figure:02d}_CMYK",
    )
    _render_filename_preview(ce.filename_template, st.session_state.library)

    if st.button("Save settings to config.json", key="cmyk_save_settings",
                 type="primary", width="content"):
        written = _persist_settings(cfg)
        if written:
            st.success(f"Saved cmyk_export to `{written}`")
        else:
            st.error("No config.json found to save into.")

    st.divider()

    # ---- Maintenance ------------------------------------------------------- #
    st.markdown("### Maintenance")
    st.caption(
        "Older save flows occasionally wrote identity (no-op) corrections "
        "like `#000000 → #000000` into per-file `cmyk_overrides` and the "
        "project-wide correction map. They look like real picks in the "
        "history dropdown but do nothing on press. Click below to strip "
        "them all in one pass."
    )
    if st.button(
        "Clean identity entries from all CMYK metadata",
        key="cmyk_cleanup_identity",
        width="content",
    ):
        store = st.session_state.store
        report = store.cleanup_identity_entries()
        if report["global"] == 0 and report["files"] == 0:
            st.info("Nothing to clean — no identity entries found.")
        else:
            st.success(
                f"Removed {report['global']} identity entr"
                f"{'y' if report['global'] == 1 else 'ies'} from the "
                f"global correction map and "
                f"{report['files']} from "
                f"{report['metadata_files_touched']} metadata file"
                f"{'' if report['metadata_files_touched'] == 1 else 's'}."
            )
