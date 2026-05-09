"""Editor tab — side-by-side preview and per-color mapping with suggestions.

Mirrors the CMYK Editor's correctness behaviors:

* Save flows strip identity picks (``target == source``) and any pick
  that already matches the global map's target — those are pure noise
  in per-file ``overrides``.
* Per-row **↺ reset** button clears that color's per-file override and
  its global-map entry in one click.
* Action row above the previews exposes the output folder.

Print-safety / luminance hints stay grayscale-only; CMYK soft-proof and
gamut warnings are CMYK-only.
"""

from __future__ import annotations

import streamlit as st

from common import (
    apply_hex_input,
    cached_color_extract,
    color_sort_key,
    color_swatch,
    fresh_mapper,
    load_semantic_palette,
    open_in_explorer,
    render_inline_svg,
    status_badge,
)
from src.color_mapper import MatchKind, gray_value, suggest_from_history
from src.library_manager import LibraryManager
from src.mapping_store import MappingStore
from src.print_safety import check_mapping
from src.semantic_palette import merge_with_semantic
from src.svg_parser import parse_svg
from src.svg_writer import apply_mapping_with_report


def _persistable_overrides(
    picks: dict[str, str],
    global_map: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Filter picks down to entries that genuinely override the global map.

    Drops two cases that should never live in per-file ``overrides``:

      * **Identity** — ``target == source``. The grayscale writer rewrites
        a color to itself, which is a no-op but pollutes history dropdowns.
      * **Already-global** — ``target == global_color_map[source].target``.
        The global map already steers this color to the same place, so a
        per-file entry is pure duplication.

    Mirrors the CMYK editor's helper of the same name.
    """
    out: dict[str, str] = {}
    for src, tgt in picks.items():
        src_u = src.upper()
        tgt_u = tgt.upper()
        if tgt_u == src_u:
            continue
        global_target = global_map.get(src_u, {}).get("target", "").upper()
        if tgt_u == global_target:
            continue
        out[src_u] = tgt_u
    return out


def render() -> None:
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

    # Surface non-sRGB color-space hints if the parser found any. The
    # full re-parse is cheap (file is already read by cached_color_extract)
    # and only runs once per Streamlit rerun.
    cs_warnings = parse_svg(svg_path).color_space_warnings
    for w in cs_warnings:
        st.warning(f"Color-space hint: {w}")

    global_map = store.load_global_map()
    mapper = fresh_mapper().with_overrides(illu.overrides)
    history = store.history()

    saved_picks = dict(st.session_state.editor_picks)
    picks: dict[str, str] = {}
    for _src in colors:
        _key = f"pick_{current}_{_src}"
        if _key in st.session_state:
            picks[_src] = st.session_state[_key].upper()
        elif _src in saved_picks:
            picks[_src] = saved_picks[_src]

    suggestions = {h: mapper.suggest(h) for h in sorted(colors)}
    effective: dict[str, str] = {}
    for src, sug in suggestions.items():
        if src in picks:
            effective[src] = picks[src]
        elif src in illu.overrides:
            effective[src] = illu.overrides[src]
        elif sug.target is not None:
            effective[src] = sug.target

    full_mapping = merge_with_semantic(
        global_map, effective, load_semantic_palette(), "grayscale",
    )
    converted_bytes, report = apply_mapping_with_report(svg_path, full_mapping)

    # ---- Action row above previews ----------------------------------------- #
    btn_open, _spacer = st.columns([1, 5])
    if btn_open.button("📂 Open output folder", key="ed_open_out", width="stretch"):
        ok, msg = open_in_explorer(cfg.paths.output_dir)
        (st.success if ok else st.error)(msg)

    left, right = st.columns(2)
    with left:
        st.markdown("**Original**")
        render_inline_svg(svg_path.read_bytes(), height=480)
    with right:
        st.markdown("**Converted (live preview)**")
        render_inline_svg(converted_bytes, height=480)

    st.divider()
    unique_dst = len(set(effective.values()))
    st.markdown(
        f"### Color mapping — {len(colors)} source · {unique_dst} unique destination"
    )

    safety_warnings = check_mapping(effective, cfg.print_safety)
    safety_targets = {w.target for w in safety_warnings}

    # Group rows by hue family then lightness — all reds together, then
    # oranges, …, neutrals last. Count is no longer the primary key (it
    # produced a salt-and-pepper order across the page).
    sorted_colors = sorted(colors.items(), key=lambda kv: color_sort_key(kv[0]))
    for idx, (src_hex, count) in enumerate(sorted_colors):
        sug = suggestions[src_hex]
        history_picks = suggest_from_history(src_hex, history)
        if idx > 0:
            st.markdown(
                "<hr style='margin:4px 0;border:none;"
                "border-top:1px solid rgba(255,255,255,0.08);'>",
                unsafe_allow_html=True,
            )
        with st.container():
            # col weights: source | match | picker | hex-input | history | safety
            row = st.columns([1, 2, 1, 2, 3, 2])
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
            # Reset: clear all mappings for this color — both per-file
            # override and the project-wide global_color_map entry. Mirrors
            # the CMYK editor's reset behavior.
            has_override = src_hex in illu.overrides
            has_global = src_hex in global_map
            badge_col, reset_col = row[1].columns([2, 1])
            badge_col.markdown(
                f"{badge}<br><small>{detail}</small>", unsafe_allow_html=True
            )
            if has_override or has_global:
                scope = (
                    "override + global" if has_override and has_global
                    else "override" if has_override
                    else "global"
                )
                if reset_col.button(
                    "↺ reset",
                    key=f"ed_reset_{current}_{src_hex}",
                    help=(
                        f"Clear all mappings for this color ({scope}). "
                        "It will fall back to suggestion / no mapping."
                    ),
                ):
                    if has_override:
                        del illu.overrides[src_hex]
                        store.save_illustration(illu)
                    if has_global:
                        store.remove_global_entry(src_hex)
                    for k in (
                        f"pick_{current}_{src_hex}",
                        f"hex_{current}_{src_hex}",
                        f"hist_{current}_{src_hex}",
                    ):
                        st.session_state.pop(k, None)
                    saved = dict(st.session_state.editor_picks)
                    saved.pop(src_hex, None)
                    st.session_state.editor_picks = saved
                    st.rerun()

            pick_key = f"pick_{current}_{src_hex}"
            hex_key = f"hex_{current}_{src_hex}"
            hist_key = f"hist_{current}_{src_hex}"

            initial = (
                picks.get(src_hex)
                or illu.overrides.get(src_hex)
                or (sug.target if sug.target else "#888888")
            )

            # Detect whether the color picker changed since the last rerun.
            current_pick_val = st.session_state.get(pick_key, initial).upper()
            prev_pick_val = (saved_picks.get(src_hex) or initial).upper()
            picker_changed = current_pick_val != prev_pick_val

            # Keep the hex text input in sync with the color picker.
            if hex_key not in st.session_state or picker_changed:
                st.session_state[hex_key] = current_pick_val

            # Reset the history selectbox whenever the color picker is changed directly.
            if picker_changed:
                st.session_state[hist_key] = "(keep current)"

            picked = row[2].color_picker(
                "target",
                value=initial,
                key=pick_key,
                label_visibility="collapsed",
            ).upper()

            row[3].text_input(
                "hex",
                key=hex_key,
                label_visibility="collapsed",
                on_change=apply_hex_input,
                args=(hex_key, pick_key),
            )

            if history_picks:
                opts = [f"{t} ({c}x)" for t, c in history_picks[:5]]
                chosen = row[4].selectbox(
                    "history",
                    options=["(keep current)"] + opts,
                    key=hist_key,
                    label_visibility="collapsed",
                )
                if chosen != "(keep current)":
                    picked = chosen.split(" ", 1)[0].upper()
            else:
                row[4].markdown("<small>no history yet</small>", unsafe_allow_html=True)

            if picked in safety_targets:
                row[5].warning("⚠ light for print")
            elif gray_value(picked) <= 16:
                row[5].caption("very dark — OK")
            else:
                row[5].caption(f"luminance {gray_value(picked)}")

            picks[src_hex] = picked

    st.session_state.editor_picks = picks

    # Persist only picks that genuinely override — drop identities and
    # picks that already match the current global map. Mirrors CMYK editor.
    real_picks = _persistable_overrides(picks, global_map)

    st.divider()
    a1, a2, a3, a4 = st.columns([1, 1, 1, 3])
    if a1.button("Save (keep status)", key="ed_save", width="content"):
        illu.overrides = dict(real_picks)
        store.save_illustration(illu)
        st.success(f"Saved {len(illu.overrides)} overrides for {current}.")
    if a2.button("Save & mark reviewed", key="ed_review", width="content", type="primary"):
        illu.overrides = dict(real_picks)
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
        for src, tgt in real_picks.items():
            store.upsert_global_entry(src, tgt, label="manual promote", notes="")
        st.success(f"Promoted {len(real_picks)} entries to the global map.")

    if report.unmapped:
        a4.warning(
            f"{len(report.unmapped)} source colors are still unmapped: "
            + ", ".join(sorted(report.unmapped)[:8])
            + ("…" if len(report.unmapped) > 8 else "")
        )
