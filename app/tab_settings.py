"""Settings tab — read-only view of resolved config (edit config.json directly)."""

from __future__ import annotations

import streamlit as st


def render() -> None:
    st.subheader("Settings")
    cfg = st.session_state.config
    st.caption(f"Config file: `{cfg.source_path}`")

    st.markdown("### Paths")
    st.write({
        "input_dir": str(cfg.paths.input_dir),
        "output_dir": str(cfg.paths.output_dir),
        "metadata_dir": str(cfg.paths.metadata_dir),
    })
    st.caption("Edit `config.json` directly to change paths, then restart the app.")

    st.markdown("### Matching")
    st.write({
        "nearest_enabled": cfg.matching.nearest_enabled,
        "metric": cfg.matching.metric,
        "threshold": cfg.matching.threshold,
    })

    st.markdown("### Print safety")
    st.write({
        "min_gray_value": cfg.print_safety.min_gray_value,
        "warn_only": cfg.print_safety.warn_only,
    })

    st.markdown("### Logging")
    st.write({"level": cfg.log_level})
