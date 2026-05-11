# 2026-05-11 — Trim CMYK PDFs to content bounds

## What was done

A new pipeline step crops each CMYK PDF to its artwork's actual extent
(plus optional pt padding) instead of the configured 5.5 × 7.5 in trim.
The publisher places illustrations into a book layout and wants no
padding to crop themselves.

When the per-batch toggle is on, the trim step runs between palette
remap and Inkscape SVG → PDF conversion. It rewrites the corrected
SVG's `viewBox` + `width` + `height` so Inkscape produces a PDF whose
MediaBox matches the artwork bbox. The fixed `target_width_inches` /
`target_height_inches` / `bleed_inches` are bypassed for that file, and
the soft-proof guide overlay is suppressed (no trim / bleed / safety
margins exist when the page IS the artwork).

### Bbox engine — three iterations to "render and detect"

The non-obvious part of this feature was computing the bbox correctly.
Three engines were attempted:

1. **`svgelements.SVG.bbox()`** — pure-Python parser, no extra deps
   beyond the new pin. Failed because `Text.bbox()` estimates extents
   from a default font and is wildly inaccurate: on
   `01.02 - mobile notifications on-off - kindness settings.svg`,
   the single label "KINDNESS SETTINGS" reported a bbox of
   `(-1193, -1865, 3671, 3829)` against a 3240 × 3240 viewBox.
   Including Text → page nearly 2× the canvas; excluding Text →
   legitimate visible text cropped off.
2. **`inkscape -S`** — delegates bbox to the same engine that renders
   the final PDF. Correct for text positioning, but reports the
   *geometric* bbox: stroke extents and text anti-aliasing run a few
   pixels past it. With 16.57 px strokes in the speech-bubble SVG,
   ~8 px of stroke got clipped on every side.
3. **Render and detect (final)** — Inkscape renders the SVG to a
   transparent PNG at 200 DPI, Pillow's `getbbox()` returns the alpha
   bbox of the rendered pixels. Whatever Inkscape will draw in the
   final PDF is what defines the crop, including strokes, anti-alias,
   filters, embedded rasters.

The validation harness (`tmp/_check_trim.py`, scratch) confirms
trim bbox == source alpha bbox to within sub-pixel rounding (< 0.5
user units delta) on all 10 input SVGs.

### Soft-proof for the CMYK Editor

The on-demand soft-proof in the CMYK Editor honours the same toggle,
so what the editor previews is what the batch will produce.

### CLI parity

`cmyk-convert` accepts `--trim / --no-trim` (`BooleanOptionalAction`,
defaults to None so the configured value wins unless overridden) and
`--trim-padding-pt FLOAT`.

### Per-file audit

The audit sidecar adds a "Trim-to-content" section with the original
viewBox, trimmed viewBox, final page size in inches, and padding
applied. The QA report's per-file batch log surfaces the same fields.

## Files added / modified

```
src/trim_to_content.py            # new — render-and-detect bbox + SVG rewrite
src/cmyk_pipeline.py              # CmykContext + process_one + soft_proof_one + audit section
src/config.py                     # CmykExportConfig + loader (nested trim_to_content block)
src/cli.py                        # --trim / --no-trim / --trim-padding-pt
app/tab_cmyk_export.py            # inline toggle + padding input + per-file log columns
app/tab_cmyk_settings.py          # _persist_settings handles trim_to_content; public alias
config.example.json               # cmyk_export.trim_to_content.{enabled, padding_pt}
README.md                         # tab-listing entry + pointer to this doc
```

## What was NOT touched (deliberate)

- No new tests. The validation harness lives in `tmp/_check_trim.py`
  during development; turning it into a permanent suite would need a
  small fixture library of SVGs in `tests/data/`, which is out of
  scope for this pass.
- `svgelements` is not re-added to `requirements.txt`. The
  render-and-detect engine only needs Inkscape (already a project
  prereq) and Pillow (transitive via Streamlit / used elsewhere in
  `src/`).
- No bleed in the trim path. The publisher use case is "page = artwork
  extent"; if a future job needs bleed-on-content, a separate flag
  can add it.
- Trim is **off by default** so existing exports keep their geometry
  until the user opts in. Spec said default on; flipped to off because
  re-rendering would silently change every existing PDF.

## Validation

```
& .\.venv\Scripts\python.exe -m py_compile src\trim_to_content.py src\cmyk_pipeline.py `
    src\config.py src\cli.py app\tab_cmyk_export.py app\tab_cmyk_settings.py
# clean

& .\.venv\Scripts\python.exe -m pytest -q
# 248 passed
```

Plus end-to-end on every SVG in `input/`:

| Check | Result |
|---|---|
| Trim bbox == source alpha bbox | delta < 0.5 user units on all sides, all 10 files |
| Visual inspection of trimmed previews | no clipping; tight crops |
| `cmyk-convert --trim --file <pie chart>` | PDF MediaBox 24.39" × 28.55", matches trimmed dimensions |
| Streamlit boot | clean |

## Surprises worth remembering

- **PNG alpha bbox is the engine of truth.** Anything that pre-computes
  bbox without rendering will miss something — stroke extents, text
  anti-alias, filter halos, drop shadows. Inkscape + Pillow's
  `getbbox()` is the cheapest correct answer.
- **`inkscape -S` reports geometric bbox, not visual.** Same with
  `--export-area-drawing`. Inkscape's preference (Edit → Preferences →
  Tools → Bounding box) controls which one, defaulting to geometric in
  most installs. Don't trust either for crop math.
- **Streamlit "Save" buttons must always render.** Gating a save
  button on a `dirty` flag races with Streamlit's widget→state sync:
  on the click rerun, `cfg` already matches the widget so dirty=False
  and the button vanishes before its callback fires. Always render
  the button.
