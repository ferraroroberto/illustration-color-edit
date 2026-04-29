"""Editor tab — side-by-side preview and per-color mapping with suggestions."""

from __future__ import annotations

import streamlit as st

from app.common import (
    cached_color_extract,
    color_swatch,
    fresh_mapper,
    render_inline_svg,
    status_badge,
)
from src.color_mapper import MatchKind, gray_value, suggest_from_history
from src.library_manager import LibraryManager
from src.mapping_store import MappingStore, merge_mappings
from src.print_safety import check_mapping
from src.svg_writer import apply_mapping_with_report


def render() -> None:
    st.subheader("Editor")
    library: LibraryManager = st.session_state.library
    store: MappingStore = st.session_state.store
    cfg = st.session_state.config

    current = st.session_state.get("current_file")
    if not current:
        st.info("No illustration selected. Pick one in the **Library** tab.")
        return

    svg_path = cfg.paths.input_dir / current
    if not svg_path.is_file():
        st.error(f"{svg_path} no longer exists. Rescan in the Library tab.")
        return

    illu = store.load_illustration(current)
    if illu.status == "pending":
        illu.with_status("in_progress")
        store.save_illustration(illu)

    st.markdown(
        f"**File:** `{current}` &nbsp; **Status:** {status_badge(illu.status)}",
        unsafe_allow_html=True,
    )

    mtime = svg_path.stat().st_mtime
    colors = cached_color_extract(str(svg_path), mtime)
    if not colors:
        st.warning("No concrete colors found in this SVG (may be all `none`/`url(...)` references).")
        return

    mapper = fresh_mapper().with_overrides(illu.overrides)
    history = store.history()

    picks: dict[str, str] = dict(st.session_state.editor_picks)

    suggestions = {h: mapper.suggest(h) for h in sorted(colors)}
    effective: dict[str, str] = {}
    for src, sug in suggestions.items():
        if src in picks:
            effective[src] = picks[src]
        elif src in illu.overrides:
            effective[src] = illu.overrides[src]
        elif sug.target is not None:
            effective[src] = sug.target

    full_mapping = merge_mappings(store.load_global_map(), effective)
    converted_bytes, report = apply_mapping_with_report(svg_path, full_mapping)

    left, right = st.columns(2)
    with left:
        st.markdown("**Original**")
        render_inline_svg(svg_path.read_bytes(), height=480)
    with right:
        st.markdown("**Converted (live preview)**")
        render_inline_svg(converted_bytes, height=480)

    st.divider()
    st.markdown(f"### Color mapping — {len(colors)} unique source colors")

    safety_warnings = check_mapping(effective, cfg.print_safety)
    safety_targets = {w.target for w in safety_warnings}

    sorted_colors = sorted(colors.items(), key=lambda kv: -kv[1])
    for src_hex, count in sorted_colors:
        sug = suggestions[src_hex]
        history_picks = suggest_from_history(src_hex, history)
        with st.container(border=True):
            row = st.columns([1, 2, 2, 3, 2])
            row[0].markdown(
                f"{color_swatch(src_hex)} <code>{src_hex}</code><br>"
                f"<small>{count} uses</small>",
                unsafe_allow_html=True,
            )

            if sug.kind is MatchKind.EXACT:
                badge = "<span style='color:#10B981'>● exact</span>"
                detail = sug.label or ""
            elif sug.kind is MatchKind.NEAR:
                badge = "<span style='color:#F59E0B'>● near</span>"
                detail = (
                    f"via <code>{sug.via}</code> · "
                    f"Δ{cfg.matching.metric.upper()}={sug.distance:.2f}"
                )
            else:
                badge = "<span style='color:#EF4444'>● none</span>"
                detail = "no exact or near match"
            row[1].markdown(f"{badge}<br><small>{detail}</small>", unsafe_allow_html=True)

            initial = (
                picks.get(src_hex)
                or illu.overrides.get(src_hex)
                or (sug.target if sug.target else "#888888")
            )
            picked = row[2].color_picker(
                "target",
                value=initial,
                key=f"pick_{current}_{src_hex}",
                label_visibility="collapsed",
            ).upper()

            if history_picks:
                opts = [f"{t} ({c}x)" for t, c in history_picks[:5]]
                chosen = row[3].selectbox(
                    "history",
                    options=["(keep current)"] + opts,
                    key=f"hist_{current}_{src_hex}",
                    label_visibility="collapsed",
                )
                if chosen != "(keep current)":
                    picked = chosen.split(" ", 1)[0].upper()
            else:
                row[3].markdown("<small>no history yet</small>", unsafe_allow_html=True)

            if picked in safety_targets:
                row[4].warning("⚠ light for print")
            elif gray_value(picked) <= 16:
                row[4].caption("very dark — OK")
            else:
                row[4].caption(f"luminance {gray_value(picked)}")

            picks[src_hex] = picked

    st.session_state.editor_picks = picks

    st.divider()
    a1, a2, a3, a4 = st.columns([1, 1, 1, 3])
    if a1.button("Save (keep status)", key="ed_save", width="content"):
        illu.overrides = {k.upper(): v.upper() for k, v in picks.items()}
        store.save_illustration(illu)
        st.success(f"Saved {len(illu.overrides)} overrides for {current}.")
    if a2.button("Save & mark reviewed", key="ed_review", width="content", type="primary"):
        illu.overrides = {k.upper(): v.upper() for k, v in picks.items()}
        illu.with_status("reviewed")
        store.save_illustration(illu)
        gm = store.load_global_map()
        new_global = 0
        for src, tgt in illu.overrides.items():
            if src not in gm:
                store.upsert_global_entry(src, tgt, label="auto-promoted from editor", notes="")
                new_global += 1
        st.success(
            f"Saved & marked reviewed. {new_global} new entries promoted to the global map."
        )
    if a3.button("Promote ALL picks to global", key="ed_promote", width="content"):
        for src, tgt in picks.items():
            store.upsert_global_entry(src, tgt, label="manual promote", notes="")
        st.success(f"Promoted {len(picks)} entries to the global map.")

    if report.unmapped:
        a4.warning(
            f"{len(report.unmapped)} source colors are still unmapped: "
            + ", ".join(sorted(report.unmapped)[:8])
            + ("…" if len(report.unmapped) > 8 else "")
        )
