"""Editor tab — side-by-side preview and per-color mapping with suggestions."""

from __future__ import annotations

import re

import streamlit as st

from common import (
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

_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def _normalize_hex(raw: str) -> str | None:
    m = _HEX_RE.match(raw.strip())
    return f"#{m.group(1).upper()}" if m else None


def _apply_hex_input(hk: str, pk: str) -> None:
    """on_change callback: push a valid hex from the text input into the color picker."""
    normalized = _normalize_hex(st.session_state.get(hk, ""))
    if normalized:
        st.session_state[pk] = normalized


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
    unique_dst = len(set(effective.values()))
    st.markdown(
        f"### Color mapping — {len(colors)} source · {unique_dst} unique destination"
    )

    safety_warnings = check_mapping(effective, cfg.print_safety)
    safety_targets = {w.target for w in safety_warnings}

    sorted_colors = sorted(colors.items(), key=lambda kv: -kv[1])
    for src_hex, count in sorted_colors:
        sug = suggestions[src_hex]
        history_picks = suggest_from_history(src_hex, history)
        with st.container(border=True):
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
            row[1].markdown(f"{badge}<br><small>{detail}</small>", unsafe_allow_html=True)

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
                on_change=_apply_hex_input,
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
