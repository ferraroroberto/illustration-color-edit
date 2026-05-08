"""CMYK Editor tab — per-illustration RGB pre-correction with side-by-side
preview and on-demand CMYK soft-proof.

Mirrors the grayscale Editor (``tab_editor.py``) in shape: same file picker
(``st.session_state.current_file``), same per-color editing UX, same global
+ per-illustration override merge. The differences:

* Reads/writes ``cmyk_overrides`` and ``cmyk_status`` instead of the
  grayscale counterparts.
* Reads/promotes from the ``cmyk_correction_map`` instead of the grayscale
  ``global_color_map``.
* No print-safety / luminance hint (those are grayscale-specific).
* Adds a "Generate CMYK soft-proof" button that runs the full Ghostscript
  pipeline once and shows the resulting PNG. The result is cached in
  ``st.session_state`` until the correction map for this file changes.
"""

from __future__ import annotations

import base64
import re

import streamlit as st

from common import (
    cached_color_extract,
    color_swatch,
    open_in_explorer,
    render_inline_svg,
    status_badge,
)
from src.color_mapper import ColorMapper, MatchKind, suggest_from_history
from src.cmyk_gamut import cmyk_gamut_delta
from src.cmyk_pipeline import CmykContext, soft_proof_one
from src.library_manager import LibraryManager
from src.mapping_store import MappingStore, merge_mappings
from src.svg_writer import apply_mapping_with_report

# Lab ΔE76 above this is "noticeably different at a glance" — the user
# probably wants to know before committing such a target.
_GAMUT_WARN_THRESHOLD = 6.0

_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def _normalize_hex(raw: str) -> str | None:
    m = _HEX_RE.match(raw.strip())
    return f"#{m.group(1).upper()}" if m else None


def _apply_hex_input(hk: str, pk: str) -> None:
    normalized = _normalize_hex(st.session_state.get(hk, ""))
    if normalized:
        st.session_state[pk] = normalized


def _persistable_overrides(
    picks: dict[str, str],
    cmyk_global: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Filter picks down to entries that genuinely override the global map.

    A pick goes into the per-file ``cmyk_overrides`` block only when it
    actually adds something the global ``cmyk_correction_map`` doesn't
    already provide. Two cases get dropped:

      * **Identity** — ``target == source``. No-op correction; the color
        already passes through unchanged.
      * **Already-global** — ``target == cmyk_correction_map[source].target``.
        The global map already steers this color to the same place, so a
        per-file entry is pure noise (and would later survive a
        "Replace globally" the user expected to delete it).

    Everything else is a real per-file pick the user wants kept.
    """
    out: dict[str, str] = {}
    for src, tgt in picks.items():
        src_u = src.upper()
        tgt_u = tgt.upper()
        if tgt_u == src_u:
            continue
        global_target = cmyk_global.get(src_u, {}).get("target", "").upper()
        if tgt_u == global_target:
            continue
        out[src_u] = tgt_u
    return out


def _build_ctx(cfg) -> CmykContext:
    return CmykContext(
        output_dir=cfg.cmyk_export.output_dir,
        icc_profile=cfg.cmyk_export.icc_profile_path,
        inkscape_exe=cfg.png_export.inkscape_path,
        ghostscript_exe=cfg.cmyk_export.ghostscript_path,
        width_inches=cfg.cmyk_export.target_width_inches,
        height_inches=cfg.cmyk_export.target_height_inches,
        bleed_inches=cfg.cmyk_export.bleed_inches,
        pdfx=cfg.cmyk_export.pdfx_compliance,
        generate_preview=True,
        preview_dpi=cfg.cmyk_export.preview_dpi,
        audit_artifacts=cfg.cmyk_export.audit_artifacts,
    )


def render() -> None:
    library: LibraryManager = st.session_state.library
    store: MappingStore = st.session_state.store
    cfg = st.session_state.config
    ce = cfg.cmyk_export

    current = st.session_state.get("current_file")
    if not current:
        st.info("No illustration selected. Pick one in the **Library** tab.")
        return

    svg_path = cfg.paths.input_dir / current
    if not svg_path.is_file():
        st.error(f"{svg_path} no longer exists. Rescan in the Library tab.")
        return

    illu = store.load_illustration(current)
    if illu.cmyk_status == "pending":
        illu.with_cmyk_status("in_progress")
        store.save_illustration(illu)

    # ---- Active configuration banner --------------------------------------- #
    # Shows exactly which ICC + dimensions + spec the soft-proof will use,
    # so the user can verify "what I'm encoding to" without leaving the tab.
    pdfx_label = "PDF/X-1a:2003" if ce.pdfx_compliance else "plain DeviceCMYK"
    st.info(
        f"**File:** `{current}` &nbsp; **Status:** {status_badge(illu.cmyk_status)}  \n"
        f"**ICC:** `{ce.icc_profile_path.name}` · "
        f"**Trim:** {ce.target_width_inches:.3f} × {ce.target_height_inches:.3f} in · "
        f"**Bleed:** {ce.bleed_inches:.3f} in · "
        f"**Spec:** {pdfx_label} · "
        f"**Output:** `{ce.output_dir}`",
    )
    mtime = svg_path.stat().st_mtime
    colors = cached_color_extract(str(svg_path), mtime)
    if not colors:
        st.warning("No concrete colors found in this SVG.")
        return

    cmyk_global = store.load_cmyk_correction_map()
    mapper = ColorMapper(global_map=cmyk_global, matching=cfg.matching).with_overrides(
        illu.cmyk_overrides
    )
    history = store.cmyk_history()

    # Per-color picks live in session state under a CMYK-specific namespace so
    # they don't collide with the grayscale Editor.
    pick_state_key = "cmyk_editor_picks"
    saved_picks = dict(st.session_state.get(pick_state_key, {}))
    picks: dict[str, str] = {}
    for src in colors:
        sk = f"cmyk_pick_{current}_{src}"
        if sk in st.session_state:
            picks[src] = st.session_state[sk].upper()
        elif src in saved_picks:
            picks[src] = saved_picks[src]

    suggestions = {h: mapper.suggest(h) for h in sorted(colors)}
    effective: dict[str, str] = {}
    for src, sug in suggestions.items():
        if src in picks:
            effective[src] = picks[src]
        elif src in illu.cmyk_overrides:
            effective[src] = illu.cmyk_overrides[src]
        elif sug.target is not None:
            effective[src] = sug.target

    full_mapping = merge_mappings(cmyk_global, effective)
    converted_bytes, report = apply_mapping_with_report(svg_path, full_mapping)

    # ---- Three-column preview row (Original | RGB-corrected | Soft-proof) -- #
    # Page is square (configured 5.5×5.5), so aspect-ratio:1/1 keeps all three
    # panels visually the same size in equal-width columns.
    page_aspect = (
        f"{ce.target_width_inches:g}/{ce.target_height_inches:g}"
        if ce.target_width_inches and ce.target_height_inches
        else "1/1"
    )

    proof_key = f"cmyk_proof_{current}"
    proof_sig_key = f"cmyk_proof_sig_{current}"
    sig = (frozenset(full_mapping.items()), ce.icc_profile_path,
           ce.pdfx_compliance, ce.target_width_inches,
           ce.target_height_inches, ce.bleed_inches)
    cached_sig = st.session_state.get(proof_sig_key)
    cached_proof = st.session_state.get(proof_key)
    proof_stale = cached_proof is not None and cached_sig != sig

    # ---- Action row above the previews ------------------------------------- #
    # Output-folder + Generate-soft-proof on the same row, so all three
    # preview columns start at the same vertical position below.
    btn_open, btn_gen, _spacer = st.columns([1, 2, 3])
    if btn_open.button("📂 Open output folder", key="cmyk_ed_open_out",
                       width="stretch"):
        ok, msg = open_in_explorer(ce.output_dir)
        (st.success if ok else st.error)(msg)
    if btn_gen.button("Generate CMYK soft-proof", key=f"sp_btn_{current}",
                      type="primary", width="stretch"):
        # Save genuine per-file picks first so the proof matches what we
        # see — but only entries that actually deviate from the global map
        # (skip identities and picks that already match the global target).
        illu.cmyk_overrides = _persistable_overrides(picks, cmyk_global)
        store.save_illustration(illu)
        ctx = _build_ctx(cfg)
        with st.spinner("Running Inkscape → Ghostscript pipeline…"):
            try:
                r = soft_proof_one(svg_path, full_mapping, ctx)
                st.session_state[proof_key] = r
                st.session_state[proof_sig_key] = sig
                cached_proof = r
                cached_sig = sig
                proof_stale = False
            except Exception as exc:  # pragma: no cover — UI safety net
                st.error(f"Soft-proof failed: {exc}")

    col_orig, col_corr, col_proof = st.columns(3)
    with col_orig:
        st.markdown("**Original**")
        render_inline_svg(svg_path.read_bytes(), aspect=page_aspect)
        st.caption("Source SVG, unmodified.")
    with col_corr:
        st.markdown("**RGB-corrected (live)**")
        render_inline_svg(converted_bytes, aspect=page_aspect)
        st.caption(
            "Browser preview of the pre-correction step. Feeds into the ICC conversion."
        )
    with col_proof:
        st.markdown("**CMYK soft-proof**")
        if cached_proof is None:
            # Empty placeholder matching the size of the other columns.
            st.markdown(
                f'<div style="background:#fafafa;border:1px dashed #d0d0d0;'
                f'border-radius:6px;width:100%;aspect-ratio:{page_aspect};'
                f'display:flex;align-items:center;justify-content:center;'
                f'color:#888;font-size:0.85em;text-align:center;padding:12px;">'
                f"Click 'Generate CMYK soft-proof' above<br>"
                f"to render the full Inkscape → Ghostscript pipeline."
                f'</div>',
                unsafe_allow_html=True,
            )
        elif cached_proof.status == "ok" and cached_proof.preview_png \
                and cached_proof.preview_png.is_file():
            _png_b64 = base64.b64encode(
                cached_proof.preview_png.read_bytes()
            ).decode()
            st.markdown(
                f'<div style="background:#fff;border:1px solid #e0e0e0;'
                f'border-radius:6px;padding:8px;width:100%;'
                f'aspect-ratio:{page_aspect};overflow:hidden;display:flex;'
                f'align-items:center;justify-content:center;">'
                f'<img src="data:image/png;base64,{_png_b64}" '
                f'style="max-width:100%;max-height:100%;object-fit:contain;">'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.caption(
                f"OK — {cached_proof.replacements} replacements, "
                f"{len(cached_proof.unmapped_colors)} unmapped, "
                f"{cached_proof.elapsed_seconds:.2f}s"
                + (" · ⚠ stale (regenerate)" if proof_stale else "")
            )
            for w in cached_proof.warnings:
                st.warning(w)
        else:
            st.error(f"Soft-proof error: {cached_proof.error}")

    # ---- Per-color editing ------------------------------------------------- #
    st.divider()
    unique_dst = len(set(effective.values()))
    st.markdown(
        f"### Color corrections — {len(colors)} source · {unique_dst} unique target"
    )

    sorted_colors = sorted(colors.items(), key=lambda kv: -kv[1])
    for src_hex, count in sorted_colors:
        sug = suggestions[src_hex]
        history_picks = suggest_from_history(src_hex, history)
        with st.container(border=True):
            # col weights: source | match | picker | hex-input | history | gamut
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
                badge = "<span style='color:#9CA3AF'>● no correction</span>"
                detail = "passes through unchanged"
            row[1].markdown(f"{badge}<br><small>{detail}</small>", unsafe_allow_html=True)

            # Reset: clear all corrections for this color — both the per-file
            # override and the project-wide cmyk_correction_map entry. The
            # color then passes straight through to ICC with no pre-correction.
            # Shown when either source of correction exists for this color.
            has_override = src_hex in illu.cmyk_overrides
            has_global = src_hex in cmyk_global
            if has_override or has_global:
                scope = (
                    "override + global" if has_override and has_global
                    else "override" if has_override
                    else "global"
                )
                if row[1].button(
                    "↺ reset",
                    key=f"cmyk_reset_{current}_{src_hex}",
                    help=(
                        f"Clear all corrections for this color "
                        f"({scope}). The color will pass through to ICC "
                        f"with no pre-correction."
                    ),
                ):
                    if has_override:
                        del illu.cmyk_overrides[src_hex]
                        store.save_illustration(illu)
                    if has_global:
                        store.remove_cmyk_correction_entry(src_hex)
                    for k in (
                        f"cmyk_pick_{current}_{src_hex}",
                        f"cmyk_hex_{current}_{src_hex}",
                        f"cmyk_hist_{current}_{src_hex}",
                    ):
                        st.session_state.pop(k, None)
                    saved = st.session_state.get(pick_state_key, {})
                    saved.pop(src_hex, None)
                    st.session_state[pick_state_key] = saved
                    st.rerun()

            pick_key = f"cmyk_pick_{current}_{src_hex}"
            hex_key = f"cmyk_hex_{current}_{src_hex}"
            hist_key = f"cmyk_hist_{current}_{src_hex}"

            initial = (
                picks.get(src_hex)
                or illu.cmyk_overrides.get(src_hex)
                or (sug.target if sug.target else src_hex)
            )

            current_pick_val = st.session_state.get(pick_key, initial).upper()
            prev_pick_val = (saved_picks.get(src_hex) or initial).upper()
            picker_changed = current_pick_val != prev_pick_val
            if hex_key not in st.session_state or picker_changed:
                st.session_state[hex_key] = current_pick_val
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

            # Gamut warning. Round-trips the *picked* target through the
            # active ICC profile and reports the resulting ΔE76. A high
            # value means the press will visibly shift this color, e.g.
            # saturated reds and pure cyans clipped by SWOP.
            delta = cmyk_gamut_delta(picked, ce.icc_profile_path)
            if delta is None:
                row[5].caption("ICC unavailable")
            elif delta >= _GAMUT_WARN_THRESHOLD:
                row[5].warning(f"⚠ CMYK shift ΔE={delta:.1f}")
            elif delta >= 2.0:
                row[5].caption(f"slight press shift ΔE={delta:.1f}")
            else:
                row[5].caption(f"in gamut ΔE={delta:.1f}")

            picks[src_hex] = picked

    st.session_state[pick_state_key] = picks

    # ---- Save buttons ------------------------------------------------------ #
    # Only persist picks that genuinely override the global map: not
    # identity (``target == source``) and not already redundant with the
    # current ``cmyk_correction_map`` entry. Saving picks that match the
    # global is pure noise — the color renders the same either way, but a
    # per-file override would later survive a "Replace globally" the user
    # thought they'd cleaned up.
    real_picks = _persistable_overrides(picks, cmyk_global)
    st.divider()
    a1, a2, a3, a4 = st.columns([1, 1, 1, 3])
    if a1.button("Save (keep status)", key="cmyk_ed_save", width="content"):
        illu.cmyk_overrides = dict(real_picks)
        store.save_illustration(illu)
        st.success(f"Saved {len(illu.cmyk_overrides)} CMYK overrides for {current}.")
    if a2.button("Save & mark reviewed", key="cmyk_ed_review", width="content", type="primary"):
        illu.cmyk_overrides = dict(real_picks)
        illu.with_cmyk_status("reviewed")
        store.save_illustration(illu)
        gm = store.load_cmyk_correction_map()
        new_global = 0
        for src, tgt in illu.cmyk_overrides.items():
            if src not in gm:
                store.upsert_cmyk_correction_entry(
                    src, tgt, label="auto-promoted from CMYK editor", notes=""
                )
                new_global += 1
        st.success(
            f"Saved & marked reviewed. {new_global} new entries promoted to "
            "the CMYK correction map."
        )
    if a3.button("Promote ALL picks to global", key="cmyk_ed_promote", width="content"):
        for src, tgt in real_picks.items():
            store.upsert_cmyk_correction_entry(
                src, tgt, label="manual promote", notes=""
            )
        st.success(
            f"Promoted {len(real_picks)} entr"
            f"{'y' if len(real_picks) == 1 else 'ies'} to the CMYK correction map."
        )

    if report.unmapped:
        a4.info(
            f"{len(report.unmapped)} colors will pass through to ICC with no "
            "pre-correction: " + ", ".join(sorted(report.unmapped)[:8])
            + ("…" if len(report.unmapped) > 8 else "")
        )
