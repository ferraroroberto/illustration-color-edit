"""
Shared UI helpers for the Streamlit tabs.

Anything that's *Streamlit-specific* but reused across more than one tab
lives here. Pure data logic stays in ``src/``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

import streamlit as st

from src.color_mapper import ColorMapper, hex_to_lab
from src.config import PROJECT_ROOT
from src.mapping_store import MappingStore
from src.palette import HUE_FAMILIES, hue_family
from src.semantic_palette import SemanticPalette, SemanticPaletteStore
from src.svg_parser import parse_svg

SEMANTIC_PALETTE_PATH = PROJECT_ROOT / "semantic-palette.json"

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Hex parsing helpers (shared by both Editor tabs)
# --------------------------------------------------------------------------- #
_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def normalize_hex(raw: str) -> str | None:
    """Return canonical ``#RRGGBB`` (uppercase) or ``None`` if invalid."""
    m = _HEX_RE.match(raw.strip())
    return f"#{m.group(1).upper()}" if m else None


def persistable_overrides(
    picks: dict[str, str],
    global_map: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Filter picks down to entries that genuinely override the global map.

    Drops two cases that should never live in per-file ``overrides``:

      * **Identity** — ``target == source``. The writer rewrites a color to
        itself, which is a no-op but pollutes history dropdowns.
      * **Already-global** — ``target == global_map[source].target``. The
        global map already steers this color to the same place, so a per-file
        entry is pure duplication (and would later survive a "Replace globally"
        the user expected to delete it).

    Used by both the grayscale Editor and the CMYK Editor — the algorithm is
    identical; only the dict they receive differs.
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


def apply_hex_input(hk: str, pk: str) -> None:
    """``on_change`` callback: copy a valid hex from a text input into a color picker.

    Used by both editors so users can type ``#3F8B5A`` into the small
    text field and have the adjacent ``st.color_picker`` follow.
    """
    normalized = normalize_hex(st.session_state.get(hk, ""))
    if normalized:
        st.session_state[pk] = normalized


_FAMILY_INDEX = {f: i for i, f in enumerate(HUE_FAMILIES)}


def color_sort_key(hex_color: str) -> tuple[int, float, float, float]:
    """Sort key that groups colors by hue family, then by lightness.

    Used to order the per-color rows in both editors so all reds sit
    together, all whites together, etc. Family order matches
    :data:`src.palette.HUE_FAMILIES` (red → orange → yellow → green →
    cyan → blue → purple → neutral). Within a family, sort by Lab L*
    (dark to light) and then by chroma to break ties deterministically.
    """
    family = hue_family(hex_color)
    L, a, b = hex_to_lab(hex_color)
    return (_FAMILY_INDEX.get(family, len(HUE_FAMILIES)), L, a, b)


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def cached_color_extract(path_str: str, mtime: float) -> dict[str, int]:
    """Return ``{hex: usage_count}`` for an SVG, keyed by path + mtime."""
    parsed = parse_svg(Path(path_str))
    return {h: u.count for h, u in parsed.colors.items()}


# --------------------------------------------------------------------------- #
# Inline rendering
# --------------------------------------------------------------------------- #
def render_inline_svg(
    svg_bytes: bytes,
    *,
    height: int = 480,
    aspect: str | None = None,
) -> None:
    """Render raw SVG bytes inline. Strips XML decl so HTML doesn't choke.

    If ``aspect`` is supplied (e.g. ``"1/1"``), the container uses
    ``width:100%`` + ``aspect-ratio`` instead of a fixed pixel height —
    useful when stacking previews in equal-width columns and you want
    them to share visible size with adjacent ``st.image`` panels.
    """
    text = svg_bytes.decode("utf-8", errors="replace")
    if text.lstrip().startswith("<?xml"):
        text = text.split("?>", 1)[1].lstrip()
    if aspect:
        size_style = f"width:100%;aspect-ratio:{aspect};"
    else:
        size_style = f"height:{height}px;"
    wrapper = (
        f'<div style="background:#fff;border:1px solid #e0e0e0;border-radius:6px;'
        f'padding:8px;{size_style}overflow:auto;display:flex;'
        f'align-items:center;justify-content:center;">{text}</div>'
    )
    st.markdown(wrapper, unsafe_allow_html=True)


def open_in_explorer(path: Path) -> tuple[bool, str]:
    """Open ``path`` in the OS file browser. Returns ``(success, message)``.

    Cross-platform: Windows uses ``os.startfile``, macOS uses ``open``,
    Linux uses ``xdg-open``. The UI calls this from a button; failures
    are surfaced to the user via the returned message rather than raising.
    """
    p = Path(path)
    if not p.exists():
        return False, f"Path does not exist: {p}"
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True, f"Opened {p}"
    except (OSError, FileNotFoundError) as exc:
        log.warning("open_in_explorer failed for %s: %s", p, exc)
        return False, f"Could not open {p}: {exc}"


def status_badge(status: str) -> str:
    color = {
        "pending": "#9CA3AF",
        "in_progress": "#F59E0B",
        "reviewed": "#10B981",
        "exported": "#3B82F6",
    }.get(status, "#9CA3AF")
    return (
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-size:0.8em;">{status}</span>'
    )


def color_swatch(hex_color: str, size: int = 22) -> str:
    return (
        f'<span style="display:inline-block;width:{size}px;height:{size}px;'
        f'background:{hex_color};border:1px solid #aaa;border-radius:3px;'
        f'vertical-align:middle;"></span>'
    )


def render_map_editor(
    load: Callable[[], dict[str, dict[str, str]]],
    usage: Callable[[], dict[str, int]],
    upsert: Callable[..., None],
    remove: Callable[[str], bool],
    *,
    key_prefix: str,
    caption: str | None = None,
    empty_message: str = "Map is empty. Map a few colors in the Editor first.",
) -> None:
    """Render a project-wide source→target color-map editor table + add form.

    Used by both the grayscale Global Map tab and the CMYK correction-map tab,
    which differ only in which store methods they bind, their widget ``key=``
    prefixes, and the surrounding copy. Pass the pipeline's bound store methods:

    * ``load`` — return the canonical-keyed map (e.g. ``store.load_global_map``).
    * ``usage`` — return per-source usage counts (e.g. ``store.usage_counts``).
    * ``upsert`` — ``(source, target, *, label, notes)`` upsert one entry.
    * ``remove`` — remove one entry by source hex.

    ``key_prefix`` namespaces every widget key (and the add-form id) so the two
    instances can coexist. ``caption`` and ``empty_message`` carry the
    per-pipeline framing copy.
    """
    if caption:
        st.caption(caption)

    gm = load()
    counts = usage()

    if not gm:
        st.info(empty_message)
    else:
        header = st.columns([1, 2, 1, 2, 3, 1])
        for i, label in enumerate(["Source", "Target", "Used in", "Label", "Notes", ""]):
            header[i].markdown(f"**{label}**")

        for src in sorted(gm):
            entry = gm[src]
            row = st.columns([1, 2, 1, 2, 3, 1])
            row[0].markdown(
                f"{color_swatch(src)} <code>{src}</code>",
                unsafe_allow_html=True,
            )
            new_target = row[1].color_picker(
                "target", value=entry["target"], key=f"{key_prefix}_t_{src}",
                label_visibility="collapsed",
            ).upper()
            row[2].write(counts.get(src, 0))
            new_label = row[3].text_input(
                "label", value=entry.get("label", ""), key=f"{key_prefix}_l_{src}",
                label_visibility="collapsed",
            )
            new_notes = row[4].text_input(
                "notes", value=entry.get("notes", ""), key=f"{key_prefix}_n_{src}",
                label_visibility="collapsed",
            )
            if row[5].button("✕", key=f"{key_prefix}_del_{src}", help="Remove entry"):
                remove(src)
                st.rerun()

            if (
                new_target != entry["target"]
                or new_label != entry.get("label", "")
                or new_notes != entry.get("notes", "")
            ):
                upsert(src, new_target, label=new_label, notes=new_notes)

    st.divider()
    st.markdown("**Add a new entry**")
    with st.form(f"{key_prefix}_add", clear_on_submit=True):
        f = st.columns([1, 1, 2, 3, 1])
        nsrc = f[0].text_input("source hex", value="#")
        ntgt = f[1].color_picker("target", value="#888888")
        nlbl = f[2].text_input("label")
        nnts = f[3].text_input("notes")
        if f[4].form_submit_button("Add"):
            if not nsrc.startswith("#") or len(nsrc) != 7:
                st.error("Source must be #RRGGBB.")
            else:
                upsert(nsrc.upper(), ntgt.upper(), label=nlbl, notes=nnts)
                st.success(f"Added {nsrc.upper()} → {ntgt.upper()}.")


def numeric_metric_cell(
    value: float | None,
    *,
    thresholds: tuple[float, float] = (2.0, 5.0),
    suffix: str = "",
    fmt: str = "{:.1f}",
) -> str:
    """Render a numeric value as a colored badge for inline-HTML cells.

    Used to show CIE ΔE between a swatch's source and its printed-appearance
    in the Palette tab. ``thresholds`` is ``(green_max, yellow_max)``:

      * ``value <= thresholds[0]`` → green (imperceptible / safe).
      * ``value <= thresholds[1]`` → yellow (perceptible — review).
      * ``value > thresholds[1]``  → red (clear shift — flag).
      * ``value is None``          → neutral grey "—".
    """
    if value is None:
        return (
            '<span style="display:inline-block;padding:1px 6px;'
            'background:#E5E7EB;color:#6B7280;border-radius:8px;'
            'font-size:0.78em;">—</span>'
        )
    g_max, y_max = thresholds
    if value <= g_max:
        bg, fg = "#10B981", "#FFFFFF"
    elif value <= y_max:
        bg, fg = "#F59E0B", "#FFFFFF"
    else:
        bg, fg = "#EF4444", "#FFFFFF"
    text = fmt.format(value) + (f" {suffix}" if suffix else "")
    return (
        f'<span style="display:inline-block;padding:1px 6px;'
        f'background:{bg};color:{fg};border-radius:8px;'
        f'font-size:0.78em;font-variant-numeric:tabular-nums;">{text}</span>'
    )


_STATUS_COLORS = {
    "pending": "#9CA3AF",
    "in_progress": "#F59E0B",
    "reviewed": "#10B981",
    "exported": "#3B82F6",
}


def compact_status_counters(label: str, counts: dict[str, int]) -> str:
    """Compact one-line "label  ● 0  ● 1  ● 0  ● 0" status counter row.

    Replaces the older pill-badge layout that wrapped awkwardly in narrow
    columns. Each status is a small colored dot followed by its count;
    statuses sit on a single non-wrapping line via ``white-space:nowrap``.
    """
    parts = [
        f'<span style="font-weight:600;margin-right:10px;">{label}</span>'
    ]
    for status in ("pending", "in_progress", "reviewed", "exported"):
        c = _STATUS_COLORS[status]
        parts.append(
            f'<span style="margin-right:14px;white-space:nowrap;" '
            f'title="{status}">'
            f'<span style="display:inline-block;width:8px;height:8px;'
            f'border-radius:50%;background:{c};margin-right:5px;'
            f'vertical-align:middle;"></span>'
            f'<span style="color:#cfcfcf;font-size:0.85em;">{status}</span>'
            f'<span style="margin-left:6px;font-variant-numeric:tabular-nums;">'
            f'{counts.get(status, 0)}</span>'
            f'</span>'
        )
    return (
        f'<div style="display:flex;flex-wrap:wrap;align-items:center;'
        f'line-height:1.8;">{"".join(parts)}</div>'
    )


# --------------------------------------------------------------------------- #
# Convenience accessors over st.session_state
# --------------------------------------------------------------------------- #
def fresh_mapper() -> ColorMapper:
    """Build a ColorMapper from current config + live global map."""
    cfg = st.session_state.config
    store: MappingStore = st.session_state.store
    return ColorMapper(global_map=store.load_global_map(), matching=cfg.matching)


def semantic_palette_store() -> SemanticPaletteStore:
    """Return the session-cached :class:`SemanticPaletteStore`.

    Loaded lazily into ``st.session_state`` so every tab gets the same
    instance and disk reads stay cheap. The store is just a path
    wrapper; the palette itself is loaded fresh via ``store.load()``
    each render so cross-tab edits show up without manual refreshes.
    """
    if "semantic_store" not in st.session_state:
        st.session_state.semantic_store = SemanticPaletteStore(SEMANTIC_PALETTE_PATH)
    return st.session_state.semantic_store


def load_semantic_palette() -> SemanticPalette:
    """Convenience: load the current semantic palette from disk."""
    return semantic_palette_store().load()
