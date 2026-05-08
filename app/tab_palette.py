"""Palette tab — curated swatch picker for the CMYK pipeline.

Lets the user converge the project onto a small shared set of colors.
The grid shows each swatch's *printed appearance* (computed by roundtripping
its source RGB through the active ICC) so picks are made by what the press
will actually produce, not by RGB hex math. "Replace globally" rewrites every
member of a swatch — across every illustration, including ones the user has
never opened — onto the swatch's ``source_hex``.

Architecture pointers:
  * Palette persists at ``<project>/palette.json`` (atomic write via
    :class:`src.palette_store.PaletteStore`).
  * The appearance cache (``Palette.appearance_cache``) is keyed by
    :func:`src.palette_store.make_icc_signature` and silently rebuilt when
    the active ICC changes — no manual "rebuild" button needed because the
    roundtrip is fast (one PIL transform per swatch, lru-cached underneath).
  * Source-color usage counts come from parsing every SVG in ``input_dir``
    (cached by ``app.common.cached_color_extract`` keyed by path+mtime).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import plotly.graph_objects as go
import streamlit as st

from common import cached_color_extract, color_swatch
from src.cmyk_gamut import cmyk_roundtrip_rgb
from src.config import PROJECT_ROOT
from src.library_manager import LibraryManager
from src.mapping_store import MappingStore, merge_mappings
from src.palette import (
    HUE_FAMILIES,
    Palette,
    Swatch,
    bucketize_for_grid,
    seed_from_hexes,
)
from src.palette_store import PaletteStore, make_icc_signature
from src.svg_writer import apply_mapping_with_report

# Cap thumbnail count so a swatch with very broad members doesn't render 200
# inline SVGs at once. The user still sees the full count above the grid.
_MAX_PREVIEW_THUMBS = 24

log = logging.getLogger(__name__)

PALETTE_PATH = PROJECT_ROOT / "palette.json"


# --------------------------------------------------------------------------- #
# Session-state-backed lazy resources
# --------------------------------------------------------------------------- #
def _palette_store() -> PaletteStore:
    if "palette_store" not in st.session_state:
        st.session_state.palette_store = PaletteStore(PALETTE_PATH)
    return st.session_state.palette_store


def _all_library_hexes(library: LibraryManager) -> list[str]:
    """Return every distinct source hex across every SVG in the library, sorted."""
    seen: set[str] = set()
    for path in library.list_svg_paths():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        try:
            colors = cached_color_extract(str(path), mtime)
        except Exception as exc:  # parse failures shouldn't kill the tab
            log.warning("Could not extract colors from %s: %s", path, exc)
            continue
        seen.update(c.upper() for c in colors)
    return sorted(seen)


def _color_usage_by_file(library: LibraryManager) -> dict[str, list[str]]:
    """Map source_hex → sorted list of illustration filenames containing it."""
    out: dict[str, list[str]] = {}
    for path in library.list_svg_paths():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        try:
            colors = cached_color_extract(str(path), mtime)
        except Exception:
            continue
        for hx in colors:
            out.setdefault(hx.upper(), []).append(path.name)
    for files in out.values():
        files.sort()
    return out


def _ensure_appearance_fresh(
    palette: Palette,
    palette_store: PaletteStore,
    icc_path: Path,
) -> str:
    """Rebuild and persist appearance_cache if stale. Returns the current ICC signature."""
    sig = make_icc_signature(icc_path)
    if not palette.swatches:
        return sig
    if palette.is_appearance_fresh(sig):
        return sig
    new_cache: dict[str, str] = {}
    for sw in palette.swatches:
        rt = cmyk_roundtrip_rgb(sw.source_hex, icc_path)
        if rt:
            new_cache[sw.id] = rt
    palette.icc_signature = sig
    palette.appearance_cache = new_cache
    palette_store.save(palette)
    return sig


# --------------------------------------------------------------------------- #
# Replace-globally implementation
# --------------------------------------------------------------------------- #
def _replace_globally_dry_run(store: MappingStore, sw: Swatch) -> tuple[int, int, list[str]]:
    """Pre-flight counts: ``(global_changes, deleted_overrides, affected_filenames)``.

    The apply step writes the swatch's ``source_hex`` into the global
    correction map and **deletes** any per-file override for the same
    members, so the global map becomes the single source of truth (future
    edits to the swatch's source_hex propagate automatically). Identity
    members (``member == source_hex``) are left alone.
    """
    global_count = 0
    deleted_count = 0
    affected_files: set[str] = set()
    cmyk_global = store.load_cmyk_correction_map()
    for member in sw.members:
        if member == sw.source_hex:
            continue
        existing = cmyk_global.get(member, {}).get("target")
        if existing != sw.source_hex:
            global_count += 1
    for illu in store.all_illustrations():
        for member in sw.members:
            if member == sw.source_hex:
                continue
            if member in illu.cmyk_overrides:
                deleted_count += 1
                affected_files.add(illu.filename)
    return global_count, deleted_count, sorted(affected_files)


def _replace_globally_apply(store: MappingStore, sw: Swatch) -> None:
    for member in sw.members:
        if member == sw.source_hex:
            continue
        store.upsert_cmyk_correction_entry(member, sw.source_hex, label=sw.label)
    for illu in store.all_illustrations():
        dirty = False
        for member in sw.members:
            if member == sw.source_hex:
                continue
            if member in illu.cmyk_overrides:
                del illu.cmyk_overrides[member]
                dirty = True
        if dirty:
            store.save_illustration(illu)


# --------------------------------------------------------------------------- #
# Visual preview (before/after thumbnails) for the confirm dialog
# --------------------------------------------------------------------------- #
def _build_after_global(
    cmyk_global: dict[str, dict[str, str]], sw: Swatch
) -> dict[str, dict[str, str]]:
    """Simulate :func:`_replace_globally_apply` on the global map only."""
    after = {k: dict(v) for k, v in cmyk_global.items()}
    for member in sw.members:
        if member == sw.source_hex:
            continue
        after[member] = {
            "target": sw.source_hex,
            "label": sw.label,
            "notes": "",
        }
    return after


def _cmyk_simulated_mapping(
    svg_path: Path,
    rgb_correction: dict[str, str],
    icc_path: Path,
) -> dict[str, str]:
    """Per-color mapping that simulates ``correction → ICC roundtrip``.

    For every concrete color in ``svg_path``, returns the sRGB hex it should
    end up displayed as if you (a) applied the RGB correction, then (b) ran
    the result through the active ICC profile and back. Useful for previewing
    "what will this look like printed?" without actually invoking Ghostscript:
    much faster (microseconds per color via the lcms2 transform cache) and
    visually nearly identical to the soft-proof for flat-color illustrations.
    """
    try:
        mtime = svg_path.stat().st_mtime
    except OSError:
        return {}
    parsed_colors = cached_color_extract(str(svg_path), mtime)
    out: dict[str, str] = {}
    for src in parsed_colors:
        src_u = src.upper()
        corrected = rgb_correction.get(src_u, src_u)
        rt = cmyk_roundtrip_rgb(corrected, icc_path)
        if rt and rt.upper() != src_u:
            out[src_u] = rt.upper()
    return out


def _visual_diffs_for_swatch(
    sw: Swatch,
    store: MappingStore,
    library: LibraryManager,
    color_to_files: dict[str, list[str]],
    icc_path: Optional[Path] = None,
) -> list[tuple[str, bytes, bytes, Optional[bytes]]]:
    """Build ``(filename, before_rgb, after_rgb, after_cmyk_or_None)`` per affected file.

    "Affected" = the source SVG contains at least one ``sw.members`` color.
    For each file:
      * ``before_rgb`` — current effective mapping (global + per-file overrides).
      * ``after_rgb`` — simulated post-apply mapping (after-global + overrides
        minus deleted members).
      * ``after_cmyk`` — ``after_rgb`` with each color additionally round-tripped
        through ``icc_path``; ``None`` if no ICC available.
    Files where the effective mapping doesn't change are skipped.
    """
    member_set = set(sw.members)
    affected_filenames: set[str] = set()
    for member in sw.members:
        affected_filenames.update(color_to_files.get(member, []))

    if not affected_filenames:
        return []

    cmyk_global_before = store.load_cmyk_correction_map()
    cmyk_global_after = _build_after_global(cmyk_global_before, sw)
    icc_available = icc_path is not None and Path(icc_path).is_file()

    file_to_path = {p.name: p for p in library.list_svg_paths()}
    diffs: list[tuple[str, bytes, bytes, Optional[bytes]]] = []
    for filename in sorted(affected_filenames):
        path = file_to_path.get(filename)
        if path is None or not path.is_file():
            continue
        illu = store.load_illustration(filename)
        before_overrides = dict(illu.cmyk_overrides)
        after_overrides = {
            k: v for k, v in illu.cmyk_overrides.items() if k not in member_set
        }
        before_mapping = merge_mappings(cmyk_global_before, before_overrides)
        after_mapping = merge_mappings(cmyk_global_after, after_overrides)
        if before_mapping == after_mapping:
            continue
        try:
            before_bytes, _ = apply_mapping_with_report(path, before_mapping)
            after_bytes, _ = apply_mapping_with_report(path, after_mapping)
            after_cmyk_bytes: Optional[bytes] = None
            if icc_available:
                # Combine 'after' RGB correction + per-color ICC roundtrip
                # into one mapping pass, so we never run the SVG writer
                # against an intermediate state.
                cmyk_map = _cmyk_simulated_mapping(path, after_mapping, icc_path)
                after_cmyk_bytes, _ = apply_mapping_with_report(path, cmyk_map)
        except Exception as exc:
            log.warning("Visual diff failed for %s: %s", filename, exc)
            continue
        diffs.append((filename, before_bytes, after_bytes, after_cmyk_bytes))
    return diffs


def _strip_xml_decl(svg_bytes: bytes) -> str:
    """Decode SVG bytes and drop the leading ``<?xml ... ?>`` so the markup
    can be embedded directly inside HTML.
    """
    text = svg_bytes.decode("utf-8", errors="replace")
    if text.lstrip().startswith("<?xml"):
        text = text.split("?>", 1)[1].lstrip()
    return text


_THUMB_WRAPPER = (
    'background:#fff;border:1px solid #eee;border-radius:4px;'
    'width:100%;aspect-ratio:1/1;display:flex;align-items:center;'
    'justify-content:center;overflow:hidden;padding:6px;box-sizing:border-box;'
)


def _thumb_cell(svg_html: str, label: str) -> str:
    """One labelled thumbnail cell inside a diff card."""
    return (
        '<div>'
        f'<div style="{_THUMB_WRAPPER}">{svg_html}</div>'
        '<div style="text-align:center;font-size:0.78em;color:#888;'
        f'margin-top:6px;">{label}</div>'
        '</div>'
    )


def _render_replace_visual_preview(
    sw: Swatch,
    store: MappingStore,
    color_to_files: dict[str, list[str]],
) -> None:
    """Render a full-width before/after card grid for the confirm dialog.

    Each card carries three thumbnails — current SVG (RGB) → proposed SVG
    (RGB) ⇒ proposed SVG simulated through the active ICC ("on press"). The
    third panel falls back to absent (and the layout collapses to two
    thumbnails) when the ICC profile isn't loadable. CSS auto-fit packs
    cards by available width with a ``minmax(880px, 1fr)`` minimum so each
    thumbnail clears editor-scale (~460px+) at typical window widths.
    """
    library: LibraryManager = st.session_state.library
    cfg = st.session_state.config
    icc_path = Path(cfg.cmyk_export.icc_profile_path)
    icc_available = icc_path.is_file()

    with st.spinner("Rendering before/after previews…"):
        diffs = _visual_diffs_for_swatch(
            sw, store, library, color_to_files,
            icc_path=icc_path if icc_available else None,
        )

    if not diffs:
        st.caption(
            "No visual changes — only the global correction-map entry is "
            "being added; no current illustration uses these colors."
        )
        return

    visible = diffs[:_MAX_PREVIEW_THUMBS]
    overflow = len(diffs) - len(visible)
    icc_note = "" if icc_available else (
        "  ·  *ICC unavailable — CMYK preview disabled*"
    )
    st.markdown(
        f"**Visual diff** — {len(diffs)} illustration"
        f"{'s' if len(diffs) != 1 else ''} change"
        f"{'' if len(diffs) != 1 else 's'}"
        + (f"  ·  *showing first {len(visible)}*" if overflow else "")
        + icc_note
    )

    cards: list[str] = []
    for filename, before_bytes, after_bytes, after_cmyk_bytes in visible:
        before_svg = _strip_xml_decl(before_bytes)
        after_svg = _strip_xml_decl(after_bytes)
        if after_cmyk_bytes is not None:
            cmyk_svg = _strip_xml_decl(after_cmyk_bytes)
            body = (
                '<div style="display:grid;'
                'grid-template-columns:1fr auto 1fr auto 1fr;'
                'gap:14px;align-items:center;">'
                + _thumb_cell(before_svg, "before (RGB)")
                + '<div style="color:#888;font-size:1.6em;font-weight:600;">→</div>'
                + _thumb_cell(after_svg, "after (RGB)")
                + '<div style="color:#888;font-size:1.6em;font-weight:600;">⇒</div>'
                + _thumb_cell(cmyk_svg, "after (on press)")
                + '</div>'
            )
        else:
            body = (
                '<div style="display:grid;grid-template-columns:1fr auto 1fr;'
                'gap:14px;align-items:center;">'
                + _thumb_cell(before_svg, "before (RGB)")
                + '<div style="color:#888;font-size:1.6em;font-weight:600;">→</div>'
                + _thumb_cell(after_svg, "after (RGB)")
                + '</div>'
            )
        cards.append(
            '<div style="background:#fff;border:1px solid #ddd;border-radius:6px;'
            'padding:14px;">'
            f'<div style="font-weight:600;margin-bottom:10px;color:#333;'
            'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"'
            f' title="{filename}">{filename}</div>'
            f'{body}'
            '</div>'
        )
    # 880px keeps three thumbnails comfortably wide on typical windows; on
    # very wide screens the auto-fit grid will pack 2 cards per row.
    container = (
        '<div style="background:#fafafa;padding:16px;border-radius:6px;'
        'border:1px solid #eee;display:grid;'
        'grid-template-columns:repeat(auto-fit, minmax(880px, 1fr));'
        'gap:16px;max-height:1100px;overflow-y:auto;">'
        + "".join(cards) +
        '</div>'
    )
    st.markdown(container, unsafe_allow_html=True)
    if overflow:
        st.caption(
            f"+{overflow} more illustration{'s' if overflow != 1 else ''} also "
            "affected (omitted from preview to keep the dialog snappy)."
        )


# --------------------------------------------------------------------------- #
# UI sections
# --------------------------------------------------------------------------- #
def _render_header(cfg, palette: Palette, sig: str) -> None:
    icc_path = Path(cfg.cmyk_export.icc_profile_path)
    cols = st.columns([3, 1, 2])
    with cols[0]:
        st.markdown(f"**Active ICC:** `{icc_path.name}`")
    with cols[1]:
        st.markdown(f"**Swatches:** {len(palette)}")
    with cols[2]:
        if not icc_path.is_file():
            st.warning("ICC profile not found — appearance previews unavailable.")
        elif not palette.swatches:
            st.caption(" ")
        elif palette.is_appearance_fresh(sig):
            st.caption("✓ Previews fresh for this ICC")
        else:
            # Should not happen — _ensure_appearance_fresh runs before this.
            st.caption("⟳ Rebuilding previews…")


def _render_seed_panel(
    palette: Palette,
    palette_store: PaletteStore,
    library: LibraryManager,
) -> None:
    expanded = not palette.swatches
    with st.expander("Seed palette from library", expanded=expanded):
        st.caption(
            "Cluster the source colors used across every SVG in the library "
            "into a fixed number of swatches. Re-running with a different k "
            "or after adding new illustrations will reshuffle the palette."
        )
        c1, c2 = st.columns([3, 1])
        k = c1.slider(
            "Number of swatches",
            min_value=5, max_value=50, value=min(30, max(5, len(palette) or 30)),
            key="palette_seed_k",
        )
        confirm_replace = (
            c1.checkbox(
                "Replace existing palette",
                value=False,
                key="palette_seed_confirm_replace",
                help="Required when the palette already has swatches.",
            )
            if palette.swatches
            else True
        )
        with c2:
            st.write("")  # vertical alignment
            st.write("")
            do_seed = st.button(
                "Generate", type="primary", key="palette_seed_btn",
                width="stretch",
            )
        if do_seed:
            if palette.swatches and not confirm_replace:
                st.error("Tick 'Replace existing palette' to re-seed.")
            else:
                hexes = _all_library_hexes(library)
                if not hexes:
                    st.error(
                        "No source colors found in input/ — add SVG illustrations first."
                    )
                else:
                    new_swatches = seed_from_hexes(hexes, k)
                    palette.replace_swatches(new_swatches)
                    palette.icc_signature = ""  # force appearance rebuild on next render
                    palette_store.save(palette)
                    st.session_state.pop("palette_selected", None)
                    st.success(
                        f"Generated {len(new_swatches)} swatch{'es' if len(new_swatches) != 1 else ''} "
                        f"from {len(hexes)} unique colors."
                    )
                    st.rerun()


def _render_palette_grid(palette: Palette, *, key: str) -> None:
    """Render the swatch grid and update ``st.session_state.palette_selected`` on click."""
    grid = bucketize_for_grid(palette.swatches)
    family_to_y = {f: i for i, f in enumerate(HUE_FAMILIES)}

    xs: list[int] = []
    ys: list[int] = []
    marker_colors: list[str] = []
    customdata: list[str] = []
    hover_text: list[str] = []
    selected_id = st.session_state.get("palette_selected")
    line_widths: list[float] = []
    line_colors: list[str] = []

    for family, row in grid.items():
        for col_idx, sw in enumerate(row):
            if sw is None:
                continue
            xs.append(col_idx)
            ys.append(family_to_y[family])
            marker_colors.append(palette.appearance_for(sw.id) or sw.source_hex)
            customdata.append(sw.id)
            hover_text.append(
                f"<b>{sw.label or '(unlabeled)'}</b><br>"
                f"src: {sw.source_hex} · members: {len(sw.members)}<br>"
                f"id: {sw.id}"
            )
            if sw.id == selected_id:
                line_widths.append(3.5)
                line_colors.append("#0066FF")
            else:
                line_widths.append(1.0)
                line_colors.append("#444")

    if not xs:
        st.info("No swatches to display.")
        return

    fig = go.Figure(
        data=go.Scatter(
            x=xs, y=ys,
            mode="markers",
            marker=dict(
                size=44,
                color=marker_colors,
                line=dict(color=line_colors, width=line_widths),
                symbol="square",
            ),
            customdata=customdata,
            hovertext=hover_text,
            hoverinfo="text",
        )
    )
    max_x = max(xs)
    fig.update_xaxes(visible=False, range=[-0.5, max_x + 0.5])
    fig.update_yaxes(
        tickmode="array",
        tickvals=list(range(len(HUE_FAMILIES))),
        ticktext=[f.capitalize() for f in HUE_FAMILIES],
        autorange="reversed",
        showgrid=False,
        zeroline=False,
    )
    fig.update_layout(
        height=max(280, len(HUE_FAMILIES) * 56),
        margin=dict(l=80, r=12, t=12, b=12),
        plot_bgcolor="#fafafa",
        paper_bgcolor="#fafafa",
        showlegend=False,
        dragmode=False,
    )

    state = st.plotly_chart(
        fig,
        on_select="rerun",
        selection_mode="points",
        key=key,
        config={"displayModeBar": False},
    )

    # Read the latest click out of the returned selection state, defensively
    # (the exact shape of `state` has shifted slightly across Streamlit
    # versions; treat it as a duck-typed dict-or-attr container).
    new_id = _extract_clicked_swatch_id(state)
    if new_id and new_id != selected_id:
        st.session_state.palette_selected = new_id
        st.rerun()


def _extract_clicked_swatch_id(state: object) -> Optional[str]:
    """Pull the customdata of the first selected point out of a PlotlyState."""
    sel = None
    if isinstance(state, dict):
        sel = state.get("selection")
    else:
        sel = getattr(state, "selection", None)
    if not sel:
        return None
    points = sel.get("points") if isinstance(sel, dict) else getattr(sel, "points", None)
    if not points:
        return None
    first = points[0]
    cd = first.get("customdata") if isinstance(first, dict) else getattr(first, "customdata", None)
    if cd is None:
        return None
    if isinstance(cd, (list, tuple)):
        return str(cd[0]) if cd else None
    return str(cd)


def _render_swatch_detail_panel(
    palette: Palette,
    palette_store: PaletteStore,
    swatch_id: str,
    color_to_files: dict[str, list[str]],
) -> None:
    """Compact swatch editor that fits in the right column next to the grid.

    Renders the preview tile, editable label/notes/source-RGB fields, and the
    members list (with per-member drill-down to illustrations). Action buttons
    — replace globally, merge, delete — live in the full-width actions section
    rendered below the columns by :func:`_render_swatch_actions`.
    """
    sw = palette.find(swatch_id)
    if sw is None:
        st.session_state.pop("palette_selected", None)
        st.rerun()
        return

    preview_hex = palette.appearance_for(sw.id) or sw.source_hex
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:14px;margin-bottom:8px;">'
        f'<div style="width:80px;height:80px;background:{preview_hex};'
        f'border:2px solid #888;border-radius:6px;"></div>'
        f'<div>'
        f'<div style="font-size:1.05rem;font-weight:600;">'
        f'{sw.label or "(unlabeled)"}</div>'
        f'<div style="color:#666;font-size:0.85rem;">'
        f'id <code>{sw.id}</code> · source <code>{sw.source_hex}</code>'
        f'</div></div></div>',
        unsafe_allow_html=True,
    )

    new_label = st.text_input("Label", value=sw.label, key=f"pal_lbl_{sw.id}")
    new_notes = st.text_area("Notes", value=sw.notes, key=f"pal_nts_{sw.id}", height=70)
    new_source = st.color_picker(
        "Source RGB (what gets injected into mappings)",
        value=sw.source_hex, key=f"pal_src_{sw.id}",
    ).upper()

    # Persist edits if anything changed.
    if (new_label, new_notes, new_source) != (sw.label, sw.notes, sw.source_hex):
        sw.label = new_label
        sw.notes = new_notes
        if new_source != sw.source_hex:
            sw.source_hex = new_source
            # Source change invalidates this swatch's appearance preview;
            # signature reset triggers the lazy rebuild on next render.
            palette.icc_signature = ""
        palette_store.save(palette)
        st.rerun()

    st.markdown("##### Members")
    if not sw.members:
        st.caption("(none — this swatch is not converging anything)")
    else:
        for m in sw.members:
            files = color_to_files.get(m, [])
            mc1, mc2, mc3 = st.columns([2, 2, 1])
            mc1.markdown(
                f"{color_swatch(m)} <code>{m}</code>", unsafe_allow_html=True
            )
            mc2.caption(
                f"{len(files)} illustration{'s' if len(files) != 1 else ''}"
            )
            with mc3.popover("Files"):
                if not files:
                    st.caption("Not present in any current SVG.")
                for fn in files:
                    if st.button(
                        fn,
                        key=f"pal_open_{sw.id}_{m}_{fn}",
                        width="stretch",
                    ):
                        st.session_state.current_file = fn
                        st.session_state.active_nav = "cmyk_editor"
                        st.rerun()


def _render_swatch_actions(
    palette: Palette,
    palette_store: PaletteStore,
    store: MappingStore,
    swatch_id: str,
    color_to_files: dict[str, list[str]],
) -> None:
    """Full-page-width actions section: replace globally + merge + delete.

    Rendered outside the swatches/detail two-column grid so the confirm
    dialog (and its visual diff) gets the full window width to breathe in,
    matching the scale of the CMYK editor's three-column preview strip.
    """
    sw = palette.find(swatch_id)
    if sw is None:
        return

    st.markdown(
        f"##### Actions  ·  <code>{sw.id}</code> "
        f"<span style='color:#666'>{sw.label or sw.source_hex}</span>",
        unsafe_allow_html=True,
    )

    pending_key = f"pal_replace_pending_{sw.id}"
    if not st.session_state.get(pending_key):
        if st.button(
            f"Replace globally  ({len(sw.members)} member"
            f"{'s' if len(sw.members) != 1 else ''})",
            type="primary",
            key=f"pal_replace_{sw.id}",
            width="stretch",
        ):
            st.session_state[pending_key] = True
            st.rerun()
    else:
        global_n, deleted_n, affected = _replace_globally_dry_run(store, sw)
        if global_n == 0 and deleted_n == 0:
            st.info("Nothing to do — every member is already pointing at this swatch.")
            if st.button("OK", key=f"pal_replace_dismiss_{sw.id}"):
                st.session_state.pop(pending_key, None)
                st.rerun()
        else:
            preview_lines = [
                f"- **{global_n}** global correction-map entr"
                f"{'y' if global_n == 1 else 'ies'} → `{sw.source_hex}`",
                f"- **{deleted_n}** per-file override"
                f"{'' if deleted_n == 1 else 's'} deleted across "
                f"**{len(affected)}** illustration"
                f"{'' if len(affected) == 1 else 's'} "
                f"(global map will then govern those colors)",
            ]
            st.warning("\n".join(preview_lines))
            if affected:
                with st.expander(
                    f"Files with overrides being cleared ({len(affected)})",
                    expanded=False,
                ):
                    st.caption(", ".join(affected))
            _render_replace_visual_preview(sw, store, color_to_files)
            cc1, cc2 = st.columns(2)
            if cc1.button(
                "Confirm replace",
                type="primary",
                key=f"pal_replace_confirm_{sw.id}",
                width="stretch",
            ):
                _replace_globally_apply(store, sw)
                st.session_state.pop(pending_key, None)
                st.success(
                    f"Replaced. {global_n} global entr"
                    f"{'y' if global_n == 1 else 'ies'} updated, "
                    f"{deleted_n} per-file override"
                    f"{'' if deleted_n == 1 else 's'} deleted."
                )
                st.rerun()
            if cc2.button(
                "Cancel", key=f"pal_replace_cancel_{sw.id}", width="stretch"
            ):
                st.session_state.pop(pending_key, None)
                st.rerun()

    # Merge + Delete on a single horizontal row spanning full width.
    other = [s for s in palette if s.id != sw.id]
    st.markdown("---")
    if other:
        mc1, mc2, mc3 = st.columns([4, 1, 1])
        merge_choices = ["(pick a swatch)"] + [
            f"{s.id} · {s.label or s.source_hex}" for s in other
        ]
        merge_pick = mc1.selectbox(
            "Merge into…",
            options=merge_choices,
            key=f"pal_merge_{sw.id}",
        )
        # Vertical filler so the buttons line up with the selectbox baseline.
        mc2.write("")
        do_merge = mc2.button(
            "Merge", key=f"pal_merge_btn_{sw.id}", width="stretch"
        )
        mc3.write("")
        do_delete = mc3.button(
            "Delete swatch",
            key=f"pal_delete_{sw.id}",
            help="Remove this swatch (does not modify any mappings).",
            width="stretch",
        )
        if do_merge:
            if merge_pick == "(pick a swatch)":
                st.error("Pick a target swatch first.")
            else:
                target_id = merge_pick.split(" ", 1)[0]
                palette.merge(target_id, sw.id)
                palette_store.save(palette)
                st.session_state.palette_selected = target_id
                st.rerun()
        if do_delete:
            palette.delete(sw.id)
            palette_store.save(palette)
            st.session_state.pop("palette_selected", None)
            st.rerun()
    else:
        if st.button(
            "Delete swatch",
            key=f"pal_delete_{sw.id}",
            help="Remove this swatch (does not modify any mappings).",
            width="stretch",
        ):
            palette.delete(sw.id)
            palette_store.save(palette)
            st.session_state.pop("palette_selected", None)
            st.rerun()


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def render() -> None:
    cfg = st.session_state.config
    store: MappingStore = st.session_state.store
    library: LibraryManager = st.session_state.library
    palette_store = _palette_store()
    palette = palette_store.load()

    icc_path = Path(cfg.cmyk_export.icc_profile_path)
    sig = _ensure_appearance_fresh(palette, palette_store, icc_path)

    _render_header(cfg, palette, sig)
    st.divider()
    _render_seed_panel(palette, palette_store, library)

    if not palette.swatches:
        st.info(
            "Palette is empty. Use the **Seed palette from library** panel "
            "above to generate a starting set from the colors in your SVGs."
        )
        return

    color_to_files = _color_usage_by_file(library)

    # Top: grid + compact swatch editor side-by-side.
    grid_col, detail_col = st.columns([3, 2], gap="large")
    with grid_col:
        st.markdown("##### Swatches  *(click to select)*")
        _render_palette_grid(palette, key="palette_grid")
    with detail_col:
        selected_id = st.session_state.get("palette_selected")
        if selected_id and palette.find(selected_id):
            _render_swatch_detail_panel(
                palette, palette_store, selected_id, color_to_files
            )
        else:
            st.caption("Click a swatch in the grid to edit it or run actions.")

    # Bottom (full window width): actions for the selected swatch. Kept out
    # of the right column on purpose so the confirm dialog's visual diff has
    # editor-scale room to render.
    selected_id = st.session_state.get("palette_selected")
    if selected_id and palette.find(selected_id):
        st.divider()
        _render_swatch_actions(
            palette, palette_store, store, selected_id, color_to_files
        )
