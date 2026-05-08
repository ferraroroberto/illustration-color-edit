# Curated Palette + Visual Diff — Design Notes

**Date:** 2026-05-08
**Author:** notes captured during the design + implementation session
**Scope:** new feature on top of the existing CMYK pipeline; a workflow-only
addition, no change to the underlying RGB-correction → ICC → PDF chain.

---

## Why this exists

The CMYK editor gives the user a Ghostscript-rendered soft-proof per
illustration, but the per-color UI only edits the *source* RGB. So tuning a
color meant blind iteration: tweak RGB → regenerate proof → eyeball it →
tweak again. That's slow on its own, and across 27 illustrations it also
produces drift — every illustration accumulates its own slightly-different
red, slightly-different blue, etc., even though the goal is a small shared
palette.

This change flips the data flow. The palette stores swatches whose
*source RGB* is known, and whose *printed appearance* is rendered through
the same `sRGB → CMYK → sRGB` ICC roundtrip the gamut warning already uses.
The user picks a swatch by appearance; the app injects the matching source
RGB into the global correction map. The same palette doubles as a
convergence dashboard: usage counts per swatch, drill-down into the
illustrations using each color, and a single "Replace globally" action that
walks every per-file override and the project-wide map in one pass.

A separate but related set of fixes addresses pollution of the CMYK
correction data: previous save flows were writing **identity entries**
(no-op corrections like `#000000 → #000000`) into both per-file
`cmyk_overrides` and the project-wide `cmyk_correction_map`. They're
visually invisible in the soft-proof (no-op = no change) but show up as
misleading suggestions in the history dropdown and inflate "Replace
globally" pre-flight counts.

## What was built

### New: `Palette` sidebar tab (under CMYK)

Lives at `app/tab_palette.py`. Three sections:

* **Header** — active ICC profile, swatch count, freshness indicator.
* **Seed panel** — k-slider (5–50) plus a "Generate" button that clusters
  every source color across every SVG in the library into k swatches via
  Lloyd's k-means in CIE Lab. The RNG seed is derived from a hash of the
  input set so the same colors produce the same swatches every time;
  adding new illustrations changes the input and triggers re-clustering.
* **Top row** — Plotly scatter grid (one square marker per swatch, marker
  color = printed appearance, custom hover tooltip with label / source /
  member count) on the left, compact swatch editor (label + notes +
  source-RGB + members list with per-color drill-down popovers) on the
  right.
* **Full-width Actions section** below the row — Replace globally
  (with pre-flight + visual diff confirm dialog), Merge into…, Delete.

### New: per-color ICC simulation

`src/cmyk_gamut.py` already had a forward+backward ICC transform pair
cached by `(profile_path, mtime)`. We extracted the roundtrip step
(`_roundtrip_rgb`) and added a sibling `cmyk_roundtrip_rgb(hex, icc_path)`
that returns the *destination* of the trip rather than the ΔE76 distance.
This is fast (microseconds per color thanks to `lcms2` + `lru_cache`), so
it's affordable to call on a whole SVG's worth of colors during a render.

The Palette tab uses this for swatch-tile colors. The visual diff (see
below) uses it to render a "what will it look like printed?" preview
without invoking Ghostscript.

### New: visual diff in the Replace-globally confirm dialog

When you click Replace globally, the confirm panel renders a card per
illustration whose appearance would actually change. Each card has three
thumbnails:

* **before (RGB)** — current effective mapping (current global +
  per-file overrides) applied to the SVG.
* **after (RGB)** — proposed mapping (after-global + overrides minus
  members) applied to the SVG.
* **after (on press)** — the same after-state with each color
  additionally pushed through the ICC roundtrip, simulating press output.
  Falls back to absent (and the layout collapses to two thumbnails) if
  the ICC profile isn't loadable.

Layout is a CSS auto-fit grid with `minmax(880px, 1fr)`, so a single
affected file stretches to full window width with editor-scale
thumbnails (~480px each); a busier diff packs cards 2-up. Capped at 24
visible cards with an overflow note.

### Changed: `Replace globally` semantics

Now **deletes** every member's per-file `cmyk_overrides` entry across
every illustration (rather than rewriting it to `swatch.source_hex`).
The global `cmyk_correction_map` becomes the single source of truth, so
future swatch source-RGB edits propagate automatically without the file
being "pinned" to a stale value.

The pre-flight reports `(global entries changing, per-file overrides
deleted, illustrations affected)` and lists the affected filenames in
an expander before commit.

### New: `↺ reset` button per row in the CMYK editor

When a color has either a per-file override or a `cmyk_correction_map`
entry, a small `↺ reset` button appears in the row's status column.
Click clears **both** the per-file override and the global config entry
for that color, so it passes straight through to ICC with no
pre-correction. Tooltip dynamically reports which scope(s) will be
affected: `(override)`, `(global)`, or `(override + global)`.

### Fixed: identity-pick pollution

Three bugs were addressed together:

1. **Save flow wrote identity picks.** `Save (keep status)`,
   `Save & mark reviewed`, and `Promote ALL picks to global` all
   persisted every row's picker value into `cmyk_overrides`,
   regardless of whether the value differed from the source — so any
   row the user didn't touch contributed an identity entry like
   `#000000 → #000000`. Same for the auto-promote step in
   "Save & mark reviewed".
2. **Generate CMYK soft-proof did the same.** It calls
   `store.save_illustration` to ensure the proof matches what the user
   sees, and was writing the unfiltered picks dict.
3. **History dropdown surfaced legacy identity entries** even after
   point 1 was fixed.

The fix is a single helper in `app/tab_cmyk_editor.py` —
`_persistable_overrides(picks, cmyk_global)` — applied at every write
site. It drops two cases:

* `target == source` (identity, no-op).
* `target == cmyk_correction_map[source].target` (already redundant
  with the global; saving a per-file entry would just shadow future
  "Replace globally" cleanups).

`src/color_mapper.suggest_from_history` now also defensively filters
identity targets so legacy data can't surface them in the dropdown
either.

### New: `MappingStore.cleanup_identity_entries()` + maintenance button

Walks every metadata file plus the `cmyk_correction_map` and strips
identity entries in a single pass. Returns counts. Exposed as a
**Clean identity entries from all CMYK metadata** button in the
*CMYK · Settings* tab — a one-shot migration for files written by
the old save flow.

## Files modified

* `src/cmyk_gamut.py` — extracted `_roundtrip_rgb`, added
  `cmyk_roundtrip_rgb(hex, icc_path)`.
* `src/palette.py` — *new*. `Swatch` and `Palette` dataclasses,
  deterministic `seed_from_hexes(hexes, k)` k-means, hue-family +
  lightness-bin grid layout (`bucketize_for_grid`), nearest-swatch
  lookup, JSON roundtrip.
* `src/palette_store.py` — *new*. Atomic `palette.json` persistence
  + `make_icc_signature(icc_path)` helper for cache invalidation.
* `src/color_mapper.py` — `suggest_from_history` filters identity
  targets.
* `src/mapping_store.py` — `cleanup_identity_entries()` migration helper.
* `app/tab_palette.py` — *new*. The Palette tab (header, seed panel,
  Plotly grid, compact swatch editor, full-width actions section,
  before/after/on-press visual diff).
* `app/tab_cmyk_editor.py` — `↺ reset` button per row;
  `_persistable_overrides` helper applied at the soft-proof button and
  all three save buttons.
* `app/tab_cmyk_settings.py` — Maintenance section with cleanup button.
* `app/app.py` — registered `cmyk_palette` destination under the CMYK
  group between Global Map and Print Export.
* `requirements.txt` — added `plotly==5.24.1` (used for the swatch
  grid; click events handled natively by Streamlit 1.55's
  `on_select="rerun"`).
* `tests/test_palette.py` — *new*. 34 tests for clustering
  determinism, hue-family classification, grid bucketing invariants,
  nearest-swatch, JSON roundtrip, store load/save, ICC signature
  staleness, `cmyk_roundtrip_rgb` (skipped without SWOP profile).
* `tests/test_color_mapper.py` — added
  `test_suggest_from_history_drops_identity_entries`.
* `tests/test_mapping_store_cmyk.py` — added
  `test_cleanup_identity_entries_strips_global_and_per_file` and
  `test_cleanup_identity_entries_noop_when_clean`.
* `.gitignore` — added `palette.json`.

## Validation

```powershell
# Syntax
& .\.venv\Scripts\python.exe -m py_compile (changed files…)

# Full test suite
& .\.venv\Scripts\python.exe -m pytest -q
# 170 passed
```

End-to-end smoke (manual):

1. Open the Palette tab. Seed at k=20. Confirm the grid renders with
   tooltips and printed-appearance colors that match the soft-proof.
2. Click a swatch → edit label/notes → confirm member counts.
3. Replace globally on a small cluster. Inspect the visual diff
   (before-RGB → after-RGB ⇒ after-on-press). Confirm.
4. Open one of the affected illustrations in the CMYK editor.
   Verify the per-file override for the affected color is gone.
   Verify the row's effective target now comes from the updated
   global map.
5. Click `↺ reset` on a row that has either an override or a global
   entry. Verify both are cleared and the row resolves to "no
   correction".
6. In CMYK · Settings, click *Clean identity entries from all CMYK
   metadata*. Verify the report counts match expectations and that
   re-clicking reports zero changes.

## Design choices worth recording

**Palette stores `source_hex`, displays printed appearance.** The
swatch's `source_hex` is the *RGB value* injected into mappings — it's
ICC-agnostic and survives a profile change. What changes when the
profile changes is only the rendered preview color (the
`appearance_cache`), which is recomputed lazily on the next render
under the new ICC. We do not attempt to invert the ICC to keep the
*appearance* stable across profile changes — that's neither reliable
(gamut clipping) nor desirable here.

**K-means initialization is deterministic.** Standard k-means picks
random initial centroids; running it twice on the same input produces
slightly different clusters. We seed `random.Random` from a SHA-256
hash of the sorted input hex set, so identical inputs always produce
byte-identical swatches. Adding a new illustration changes the input
set, so the seed (and clustering) change as expected. Documented in
`tests/test_palette.py::test_seed_is_deterministic_for_same_input` and
`test_seed_input_order_does_not_change_result`.

**Replace globally deletes overrides instead of rewriting them.**
Considered three options:

* *Delete per-file override.* Global map becomes single source of
  truth; future swatch source-RGB edits propagate. Chosen.
* *Rewrite per-file override to swatch source_hex.* File stays
  "pinned" to whatever the source_hex was at replace-time; future
  global edits don't propagate.
* *Leave per-file overrides alone.* Conservative, but defeats
  convergence for already-edited files.

The user explicitly picked delete; documented in this doc and the
in-code comment on `_replace_globally_apply`.

**On-press preview uses per-color ICC roundtrip rather than
Ghostscript.** Running the full Ghostscript pipeline (Inkscape SVG →
RGB PDF → CMYK PDF → preview PNG) takes ~1.3 s per illustration.
Running it twice (before + after) for every affected file in a
"Replace globally" diff is too slow to put behind a confirm dialog
button. Per-color `sRGB → CMYK → sRGB` via `PIL.ImageCms` is
microseconds per color and visually nearly identical for flat-color
illustrations like ours. The trade-off: the per-color simulation
doesn't account for overprint, transparency blending, or paper white
— which don't apply to our SVGs. The full Ghostscript proof remains
available in the CMYK editor for any single file the user wants to
inspect at full fidelity.
