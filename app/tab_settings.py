"""Grayscale Settings tab.

Mirrors :mod:`tab_cmyk_settings` in shape: read-only summary at the top,
editable form below, then a Maintenance section.

Grayscale settings are split across two files:

* ``config.json``       — ``paths``, ``png_export``
* ``color-config.json`` — ``matching``, ``print_safety``, ``logging``

The save helpers below write each value back to its real file in place;
the ``.example`` fallbacks are never written to.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from src.config import PROJECT_ROOT


def _write_json_in_place(path: Path, mutate) -> Path | None:
    """Read ``path``, run ``mutate(raw_dict)``, write back. Returns path or None."""
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    mutate(raw)
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return path


def _persist_paths_and_png(cfg) -> Path | None:
    """Write paths + png_export back to config.json in place."""
    cfg_path = cfg.source_path or (PROJECT_ROOT / "config.json")
    # cfg.source_path can point to color-config.json — coerce to config.json.
    if cfg_path.name != "config.json":
        cfg_path = PROJECT_ROOT / "config.json"

    def _mutate(raw):
        raw["paths"] = {
            "input_dir": str(cfg.paths.input_dir),
            "output_dir": str(cfg.paths.output_dir),
            "metadata_dir": str(cfg.paths.metadata_dir),
        }
        raw["png_export"] = {
            "enabled": cfg.png_export.enabled,
            "dpi": cfg.png_export.dpi,
            "inkscape_path": cfg.png_export.inkscape_path,
        }

    return _write_json_in_place(cfg_path, _mutate)


def _persist_matching_and_safety(cfg) -> Path | None:
    """Write matching + print_safety back to color-config.json in place."""
    color_path = PROJECT_ROOT / "color-config.json"

    def _mutate(raw):
        raw["matching"] = {
            "nearest_enabled": cfg.matching.nearest_enabled,
            "metric": cfg.matching.metric,
            "threshold": cfg.matching.threshold,
        }
        raw["print_safety"] = {
            "min_gray_value": cfg.print_safety.min_gray_value,
            "warn_only": cfg.print_safety.warn_only,
        }

    return _write_json_in_place(color_path, _mutate)


def render() -> None:
    cfg = st.session_state.config

    # ---- Active configuration (read-only summary) -------------------------- #
    st.markdown("### Active configuration")
    st.caption(f"Config files: `{PROJECT_ROOT / 'config.json'}` + `{PROJECT_ROOT / 'color-config.json'}`")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Paths**")
        st.markdown(
            f"Input: `{cfg.paths.input_dir}`  \n"
            f"Output: `{cfg.paths.output_dir}`  \n"
            f"Metadata: `{cfg.paths.metadata_dir}`"
        )

        st.markdown("**Matching**")
        st.markdown(
            f"Nearest enabled: **{cfg.matching.nearest_enabled}**  \n"
            f"Metric: **{cfg.matching.metric}**  \n"
            f"Threshold: **{cfg.matching.threshold}**"
        )

    with c2:
        st.markdown("**Print safety**")
        st.markdown(
            f"Min gray value: **{cfg.print_safety.min_gray_value}**  \n"
            f"Warn only: **{cfg.print_safety.warn_only}**"
        )

        st.markdown("**PNG export**")
        st.markdown(
            f"Enabled: **{cfg.png_export.enabled}**  \n"
            f"DPI: **{cfg.png_export.dpi}**  \n"
            f"Inkscape: `{cfg.png_export.inkscape_path}`"
        )

        st.markdown("**Logging**")
        st.markdown(f"Level: **{cfg.log_level}**")

    st.divider()

    # ---- Editable form ----------------------------------------------------- #
    st.markdown("### Edit settings")
    st.caption(
        "Changes apply to this session immediately. Use the per-section "
        "**Save** buttons to persist to disk."
    )

    st.markdown("##### Paths & PNG export *(config.json)*")
    cfg.paths.input_dir = Path(st.text_input(
        "Input directory", value=str(cfg.paths.input_dir),
        key="settings_input_dir",
    ))
    cfg.paths.output_dir = Path(st.text_input(
        "Output directory", value=str(cfg.paths.output_dir),
        key="settings_output_dir",
    ))
    cfg.paths.metadata_dir = Path(st.text_input(
        "Metadata directory", value=str(cfg.paths.metadata_dir),
        key="settings_metadata_dir",
    ))
    p1, p2, p3 = st.columns(3)
    cfg.png_export.enabled = p1.checkbox(
        "PNG export enabled", value=cfg.png_export.enabled,
        key="settings_png_enabled",
    )
    cfg.png_export.dpi = p2.number_input(
        "PNG DPI", min_value=72, max_value=1200,
        value=int(cfg.png_export.dpi), step=24,
        key="settings_png_dpi",
    )
    cfg.png_export.inkscape_path = p3.text_input(
        "Inkscape binary (path or name on PATH)",
        value=cfg.png_export.inkscape_path,
        key="settings_inkscape",
    )
    if st.button(
        "Save paths + PNG to config.json",
        key="settings_save_paths",
        type="primary",
        width="content",
    ):
        written = _persist_paths_and_png(cfg)
        if written:
            st.success(f"Saved paths + PNG export to `{written}`")
        else:
            st.error("config.json not found — nothing written.")

    st.markdown("##### Matching & print safety *(color-config.json)*")
    m1, m2, m3 = st.columns(3)
    cfg.matching.nearest_enabled = m1.checkbox(
        "Nearest-color enabled", value=cfg.matching.nearest_enabled,
        key="settings_nearest_enabled",
    )
    cfg.matching.metric = m2.selectbox(
        "Distance metric", options=["lab", "rgb"],
        index=0 if cfg.matching.metric == "lab" else 1,
        key="settings_metric",
    )
    cfg.matching.threshold = m3.number_input(
        "Threshold", min_value=0.0, max_value=200.0,
        value=float(cfg.matching.threshold), step=0.5,
        key="settings_threshold",
    )
    s1, s2 = st.columns(2)
    cfg.print_safety.min_gray_value = s1.text_input(
        "Min gray value (#RRGGBB)",
        value=cfg.print_safety.min_gray_value,
        key="settings_min_gray",
    ).upper()
    cfg.print_safety.warn_only = s2.checkbox(
        "Warn only (don't fail CLI)", value=cfg.print_safety.warn_only,
        key="settings_warn_only",
    )
    if st.button(
        "Save matching + print safety to color-config.json",
        key="settings_save_color",
        type="primary",
        width="content",
    ):
        written = _persist_matching_and_safety(cfg)
        if written:
            st.success(f"Saved matching + print safety to `{written}`")
        else:
            st.error(
                "color-config.json not found — copy it from "
                "color-config.json.example first."
            )

    st.divider()

    # ---- Maintenance ------------------------------------------------------- #
    st.markdown("### Maintenance")
    st.caption(
        "Older save flows occasionally wrote identity (no-op) mappings "
        "like `#000000 → #000000` into per-file `overrides` and the "
        "project-wide `global_color_map`. They look like real picks in "
        "the history dropdown but do nothing on output. Click below to "
        "strip them all in one pass."
    )
    if st.button(
        "Clean identity entries from all grayscale metadata",
        key="settings_cleanup_identity",
        width="content",
    ):
        store = st.session_state.store
        report = store.cleanup_identity_entries(pipeline="grayscale")
        if report["global"] == 0 and report["files"] == 0:
            st.info("Nothing to clean — no identity entries found.")
        else:
            st.success(
                f"Removed {report['global']} identity entr"
                f"{'y' if report['global'] == 1 else 'ies'} from the "
                f"global color map and "
                f"{report['files']} from "
                f"{report['metadata_files_touched']} metadata file"
                f"{'' if report['metadata_files_touched'] == 1 else 's'}."
            )
