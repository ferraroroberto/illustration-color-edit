"""Accessibility tab — color-blind simulation across the library.

Two modes:

* **Library strip** — one row per illustration showing original +
  grayscale-output + the four CB simulations side by side. Lets you
  scan every illustration at once. A red dot above any sim cell means
  that illustration loses meaningful color contrast for that CB
  audience.
* **Per-illustration grid** — pick one illustration, see all six
  panes large. Useful for nailing down which color pair is collapsing.

The simulation works at the **SVG level** (color substitution + inline
re-render) — much faster than rasterizing each illustration through
Inkscape just to apply a 3×3 matrix. Achromat / deutan / protan / tritan
all render as crisp vectors at any size.

The CMYK soft-proof is intentionally **not** included here — running a
Ghostscript pipeline per CB type per illustration would be prohibitively
slow. The grayscale output is a good proxy for "does the ordering
survive print?" and runs the same way it does in the rest of the app.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from common import cached_color_extract, render_inline_svg
from src.colorblind import (
    CB_TYPES,
    POPULATION_PCT,
    assess_risk,
    simulate_mapping,
)
from src.library_manager import LibraryManager
from src.svg_writer import apply_mapping_with_report


def _simulated_svg_bytes(
    svg_path: Path,
    cb_type: str,
    severity: float,
) -> bytes:
    """Return the SVG bytes after CB simulation. Pass-through for ``normal``."""
    if cb_type == "normal":
        return svg_path.read_bytes()
    try:
        mtime = svg_path.stat().st_mtime
    except OSError:
        return svg_path.read_bytes()
    palette = cached_color_extract(str(svg_path), mtime)
    sim_map = simulate_mapping(palette.keys(), cb_type, severity)  # type: ignore[arg-type]
    if not sim_map:
        return svg_path.read_bytes()
    body, _ = apply_mapping_with_report(svg_path, sim_map)
    return body


@st.cache_data(show_spinner=False)
def _risk_for_path(path_str: str, mtime: float, severity: float) -> dict:
    """Cached risk assessment per (file, severity)."""
    palette = cached_color_extract(path_str, mtime)
    r = assess_risk(palette.keys(), severity=severity)
    return {
        "deutan": r.deutan,
        "protan": r.protan,
        "tritan": r.tritan,
        "achromat": r.achromat,
        "collapsed_pairs": r.collapsed_pairs,
    }


def _risk_dot(active: bool) -> str:
    color = "#EF4444" if active else "#10B981"
    label = "at risk" if active else "ok"
    return (
        f'<span title="{label}" style="display:inline-block;width:10px;'
        f'height:10px;border-radius:50%;background:{color};'
        f'margin-right:6px;vertical-align:middle;"></span>'
    )


def _column_header(cb_type: str) -> str:
    if cb_type == "normal":
        return "**Original**"
    if cb_type == "achromat":
        return f"**Achromat**  <small>({POPULATION_PCT['achromat']})</small>"
    return (
        f"**{cb_type.capitalize()}**  "
        f"<small>({POPULATION_PCT[cb_type]})</small>"
    )


def _render_filter_panel(library: LibraryManager) -> tuple[float, list[str], bool]:
    cols = st.columns([2, 3, 2])
    with cols[0]:
        severity = st.slider(
            "Severity",
            min_value=0.5, max_value=1.0, value=1.0, step=0.05,
            key="acc_severity",
            help="0.5 = mild deficiency, 1.0 = full dichromacy.",
        )
    with cols[1]:
        cb_chosen = st.multiselect(
            "Show simulations",
            options=list(CB_TYPES),
            default=list(CB_TYPES),
            format_func=lambda t: t.capitalize(),
            key="acc_cb_types",
        )
    with cols[2]:
        only_affected = st.checkbox(
            "Show only affected illustrations",
            value=False,
            key="acc_only_affected",
            help="Hide illustrations that survive every CB type cleanly.",
        )
    return severity, cb_chosen, only_affected


def _render_strip_mode(
    library: LibraryManager,
    severity: float,
    cb_chosen: list[str],
    only_affected: bool,
) -> None:
    paths = library.list_svg_paths()
    if not paths:
        st.info("No SVGs in `input/`.")
        return

    columns_to_show = ["normal"] + list(cb_chosen)
    header_cols = st.columns(len(columns_to_show))
    for col, cb in zip(header_cols, columns_to_show):
        col.markdown(_column_header(cb), unsafe_allow_html=True)
    st.divider()

    shown = 0
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        risk = _risk_for_path(str(path), mtime, severity)
        if only_affected and not any(risk.get(cb) for cb in cb_chosen):
            continue
        shown += 1
        st.markdown(f"**{path.name}**")
        cols = st.columns(len(columns_to_show))
        for col, cb in zip(cols, columns_to_show):
            with col:
                bytes_ = _simulated_svg_bytes(path, cb, severity)
                render_inline_svg(bytes_, height=180)
                if cb != "normal" and risk.get(cb):
                    st.markdown(_risk_dot(True) + "at risk", unsafe_allow_html=True)
        st.divider()

    if shown == 0:
        st.success(
            "No illustrations are flagged at the current severity — color "
            "ordering survives all simulations."
        )


def _render_per_illustration(
    library: LibraryManager,
    severity: float,
    cb_chosen: list[str],
) -> None:
    paths = library.list_svg_paths()
    if not paths:
        st.info("No SVGs in `input/`.")
        return
    options = [p.name for p in paths]
    default = (
        options.index(st.session_state.current_file)
        if st.session_state.get("current_file") in options
        else 0
    )
    chosen = st.selectbox(
        "Pick an illustration",
        options=options,
        index=default,
        key="acc_picked_file",
    )
    path = next(p for p in paths if p.name == chosen)
    mtime = path.stat().st_mtime
    risk = _risk_for_path(str(path), mtime, severity)

    if not any(risk.get(cb) for cb in CB_TYPES):
        st.success("No CB simulation collapses a meaningful color pair here.")
    else:
        affected = ", ".join(t for t in CB_TYPES if risk.get(t))
        st.warning(f"Affected CB types: **{affected}**")

    columns_to_show = ["normal"] + list(cb_chosen)
    cols = st.columns(len(columns_to_show))
    for col, cb in zip(cols, columns_to_show):
        with col:
            st.markdown(_column_header(cb), unsafe_allow_html=True)
            bytes_ = _simulated_svg_bytes(path, cb, severity)
            render_inline_svg(bytes_, height=420)
            if cb != "normal" and risk.get(cb):
                st.markdown(_risk_dot(True) + "at risk", unsafe_allow_html=True)

    if risk["collapsed_pairs"]:
        st.markdown("##### Collapsed pairs (original ΔE → simulated ΔE)")
        for cb, a, b, orig, sim in risk["collapsed_pairs"]:
            st.markdown(
                f"- **{cb}**: `{a}` ↔ `{b}` — {orig:.1f} → {sim:.1f}"
            )


def render() -> None:
    library: LibraryManager = st.session_state.library
    st.markdown(
        "Simulate how colorblind readers see each illustration. The check "
        "runs on the **source SVG** (and its grayscale output via the "
        "Achromat sim) — no Ghostscript pipeline involved, so it's fast "
        "across the whole library."
    )

    mode = st.radio(
        "Mode",
        options=("Library strip", "Single illustration"),
        horizontal=True,
        key="acc_mode",
    )
    severity, cb_chosen, only_affected = _render_filter_panel(library)
    st.divider()
    if mode == "Library strip":
        _render_strip_mode(library, severity, cb_chosen, only_affected)
    else:
        _render_per_illustration(library, severity, cb_chosen)
