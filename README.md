# Illustration Color Edit

Industrial-grade SVG conversion pipelines for book illustrations where color
carries semantic meaning (red = bad, green = good, etc.). Two parallel
pipelines on the same source SVGs:

1. **Color → grayscale** for the print-grayscale edition of the body of the book.
2. **Color → CMYK PDF** for publisher delivery of color illustrations at specific
   trim dimensions.

The tool consists of:

- A **Streamlit app** (`app/app.py`) for interactive per-illustration mapping with
  side-by-side preview and suggestion engine.
- A **CLI batch processor** (`src/cli.py`) for converting an entire library once
  mappings are locked in. Supports both pipelines.

Source illustrations are authored in Affinity Designer 2 (which has no scripting
API), so the workflow is an SVG round-trip: export from Affinity → process here →
re-open the processed SVG in Affinity.

## Why this exists

A typical book illustration in this project has 100+ colored data points. The
print edition is grayscale, and color carries semantic weight (e.g. red = worse,
green = better, yellow = warning). A naive RGB→luma conversion destroys that
ordering. This app lets you:

1. Define a **global** "this red always becomes this gray" mapping once.
2. Reuse it across the whole book library.
3. Override per illustration when needed.
4. Preview side-by-side before committing.
5. Batch-export the whole `input/` folder when mappings are final.

## Print theory cheatsheet

The CMYK pipeline plus the Accessibility tab make several press-side checks
that aren't obvious if you've only worked in RGB. A quick map from
prepress vocabulary to what this tool does:

- **ICC profile.** A vendor-supplied 3D lookup that converts colors between
  spaces (here: sRGB → CMYK → sRGB). Soft-proofs and gamut math both ride on
  this. Pick the profile your printer specifies; default to **ISO Coated v2
  (ECI)** for EU coated stock, **U.S. Web Coated SWOP v2** for North American
  coated.
- **CIE ΔE76.** Lab-space distance between two colors. ΔE < 1 imperceptible,
  2–5 visible if you look, > 5 obviously different. The Palette tab shows
  ΔE per swatch (source vs. printed appearance) and the Accessibility tab
  uses it to detect color-blindness pair collapse.
- **Total Area Coverage (TAC).** The per-pixel sum of CMYK ink, in percent
  (0–400). Coated stock typically caps at 300–340%; uncoated at 240–280%;
  newsprint at ~220%. Files that exceed the limit get rejected by prepress.
  This tool measures `max / mean / p99 / violation%` per CMYK PDF.
- **Force-K (100% K only).** Thin near-black lines and small near-black text
  should print on the K plate alone. Rendered as four-color black, they pick
  up colored fringing from press misregistration. Detection always runs;
  per-file `cmyk_auto_fix` enables Ghostscript's `-dBlackText -dBlackVector`
  fix.
- **Trim / bleed / safety.** Concentric rectangles. Trim is the cut line.
  Bleed is *outside* trim (3–5 mm); art that should reach the page edge must
  extend to here. Safety is *inside* trim (4–5 mm); critical content stays
  inside. The soft-proof draws all three.
- **PDF/X-1a:2003.** A stricter PDF spec publishers prefer (or require). It
  forbids transparency and embeds the OutputIntent (i.e. the ICC profile)
  inside the PDF so the printer's RIP can't pick the wrong one.
- **Color-blindness simulation (Machado 2009).** Linear-RGB matrix
  transforms parameterized by severity (0–1) for the three common cone
  deficiencies. Achromat is BT.709 luma. Used to flag illustrations whose
  semantic ordering (red=bad / green=good) collapses for ~8% of male
  readers.
- **Semantic palette.** A layer above hex mappings: bind authored colors
  to named slots (`status.bad`, `status.good`, …) and slots to per-pipeline
  targets within a theme. Re-skinning for a different publisher = swap
  themes, no per-illustration touching.

For the *why* behind each check, see
[`docs/2026-05-09-publisher-grade-additions.md`](docs/2026-05-09-publisher-grade-additions.md).

## Project structure

```
illustration-color-edit/
├── app/
│   ├── app.py                  # Streamlit entry point (scaffolding + tab routing)
│   ├── common.py               # shared Streamlit helpers (swatches, badges, caching)
│   ├── tab_library.py          # Library tab
│   ├── tab_semantic_palette.py # Semantic Palette tab (slots + themes)
│   ├── tab_accessibility.py    # Accessibility tab (color-blind simulation + risk)
│   ├── tab_editor.py           # Editor tab (grayscale)
│   ├── tab_global_map.py       # Global Map tab (grayscale)
│   ├── tab_batch.py            # Batch Export tab (grayscale)
│   ├── tab_cmyk_editor.py      # CMYK Editor tab (per-file + auto-fix toggle + soft-proof)
│   ├── tab_cmyk_global_map.py  # CMYK correction map tab
│   ├── tab_palette.py          # Palette tab (curated swatch picker + ΔE + visual diff)
│   ├── tab_cmyk_export.py      # CMYK Print Export tab (batch + delivery snapshot button)
│   ├── tab_cmyk_settings.py    # CMYK Settings tab (TAC, force-K, guides, template…)
│   └── tab_settings.py         # Settings tab
├── src/
│   ├── svg_parser.py       # extract colors, parse <style> blocks, sRGB sanity check
│   ├── color_mapper.py     # exact + nearest-color matching, suggestion engine
│   ├── svg_writer.py       # apply mappings, write output SVG
│   ├── library_manager.py  # scan input dir, track per-file status
│   ├── mapping_store.py    # global + per-illustration mapping persistence (both pipelines)
│   ├── semantic_palette.py # named slots + themes layered over hex mappings
│   ├── print_safety.py     # gray-value warnings for uncoated paper
│   ├── svg_to_pdf.py       # Inkscape wrapper: SVG → RGB PDF at trim size
│   ├── cmyk_convert.py     # Ghostscript wrapper: RGB PDF → CMYK PDF via ICC
│   ├── cmyk_pipeline.py    # CMYK pipeline orchestrator (single + batch + soft-proof)
│   ├── cmyk_tac.py         # Total Area Coverage check (max/mean/p99 per page)
│   ├── force_k.py          # fine-line + small-text detector for K-plate forcing
│   ├── bleed_overlay.py    # composite trim/bleed/safety guides on soft-proof PNGs
│   ├── filename_template.py# parse <chapter>.<figure> + apply output naming template
│   ├── colorblind.py       # Machado-2009 simulation + risk assessment
│   ├── palette.py          # curated palette: Swatch/Palette + Lab k-means seeding
│   ├── palette_store.py    # palette.json persistence + ICC signature helper
│   ├── cmyk_gamut.py       # ICC roundtrip helpers (gamut warning + printed-appearance preview)
│   ├── qa_report.py        # CMYK batch HTML QA report writer (TAC + force-K columns)
│   ├── delivery.py         # snapshot project state into deliveries/ for handoff
│   └── cli.py              # batch CLI entry point (both pipelines + deliver)
├── tests/                      # pytest unit tests
├── docs/                       # design docs and changelog entries
├── tmp/                        # working scratch space (gitignored except .gitkeep)
├── profiles/                   # ICC profiles — drop in here, gitignored
├── input/                      # source SVGs — gitignored, path set in config.json
├── output/                     # converted grayscale SVGs/PNGs — gitignored
├── output_cmyk/                # converted CMYK PDFs + QA report — gitignored
├── metadata/                   # per-illustration .mapping.json — gitignored
├── config.example.json         # committed template for folder paths + cmyk_export
├── config.json                 # gitignored, your local folder paths
├── color-config.json.example   # committed template for color + cmyk correction maps
├── color-config.json           # gitignored, your local color mappings
├── palette.json                # gitignored, curated CMYK palette (auto-created by Palette tab)
├── semantic-palette.json       # gitignored, slot bindings + themes (auto-created by Semantic Palette tab)
├── deliveries/                 # gitignored, per-handoff snapshots (configs + PDFs + manifest)
├── .env.example
├── .gitignore
├── requirements.txt
├── launch_app.bat
└── README.md
```

## Prerequisites

- **Python 3.11+**
- **Inkscape 1.x** — required for PNG export (grayscale pipeline) and
  SVG → PDF (CMYK pipeline). Install from [inkscape.org](https://inkscape.org).
  On Windows the default path is `C:\Program Files\Inkscape\bin\inkscape.exe`; set
  `png_export.inkscape_path` in `config.json` if it differs.
- **Ghostscript 10+** — *only* required for the CMYK pipeline. Skip if you
  only use grayscale.
  - **Windows**: download `gswin64c.exe` installer from
    [ghostscript.com/releases](https://ghostscript.com/releases/gsdnld.html).
    Adds `gswin64c` to PATH automatically. Confirm with `gswin64c --version`.
  - **macOS**: `brew install ghostscript`.
  - **Linux**: `apt install ghostscript` (Debian/Ubuntu) or
    `dnf install ghostscript` (Fedora/RHEL).
  - If `gswin64c` is not on PATH, set `cmyk_export.ghostscript_path` in
    `config.json` to the absolute binary path.
- **An ICC profile** — *only* required for the CMYK pipeline. The publisher
  may specify one; if not, default to **ISO Coated v2 (ECI)** for EU print:
  free from [eci.org](https://www.eci.org). Drop the `.icc` file into
  `profiles/` and point `cmyk_export.icc_profile_path` at it.

## Setup (Windows + PowerShell)

```powershell
# from the project root
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# create your real configs from the templates
copy config.example.json config.json
copy color-config.json.example color-config.json
copy .env.example .env
```

## Setup (POSIX)

```bash
python -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

cp config.example.json config.json
cp color-config.json.example color-config.json
cp .env.example .env
```

## Configuration

The app uses two gitignored config files with committed `.example` counterparts.

### `config.json` — folder paths

Points to where your SVG files live. Paths can be relative (to the project
root) or absolute.

```jsonc
{
  "paths": {
    "input_dir": "./input",    // source SVGs (Affinity exports)
    "output_dir": "./output",  // converted grayscale SVGs + PNGs
    "metadata_dir": "./metadata"  // per-illustration mapping overrides
  },
  "png_export": {
    "enabled": true,            // also write a .png alongside each output .svg
    "dpi": 300,                 // rasterisation resolution (300 = standard print)
    "inkscape_path": "inkscape" // full path if inkscape is not on PATH
  },
  "cmyk_export": {
    "enabled": true,
    "output_dir": "./output_cmyk",                          // CMYK PDFs land here
    "icc_profile_path": "./profiles/ISOcoated_v2_eci.icc",  // see Prerequisites
    "ghostscript_path": "gswin64c",                         // or absolute path
    "target_width_inches": 5.5,                             // trim width
    "target_height_inches": 7.5,                            // trim height
    "bleed_inches": 0.0,                                    // bleed on all sides
    "pdfx_compliance": false,                               // PDF/X-1a:2003 (forbids transparency)
    "generate_preview_png": true,                           // soft-proof PNG per file
    "preview_dpi": 150,                                     // soft-proof resolution
    "audit_artifacts": true,                                // write `<name>_CMYK_report.txt` (and keep `.pdfx_def.ps`) per file
    "filename_template": "",                                // empty = `<stem>_CMYK.pdf` default
    "tac_limit_percent": 320.0,                             // Total Area Coverage cap; 320 typical coated, 240–280 uncoated
    "tac_check_dpi": 100,                                   // resolution at which TAC is sampled
    "force_k_min_stroke_pt": 0.5,                           // strokes ≤ this (pt) get flagged as fine-line
    "force_k_min_text_pt": 9.0,                             // text smaller than this (pt) gets flagged
    "safety_inches": 0.1875,                                // safety margin inset from trim (≈ 4.76 mm)
    "show_guide_overlay": true                              // composite trim/bleed/safety guides on the soft-proof PNG
  }
}
```

### `color-config.json` — color mappings and parameters

```jsonc
{
  "global_color_map": {
    "#E74C3C": { "target": "#333333", "label": "red / bad", "notes": "..." }
  },
  "cmyk_correction_map": {
    // RGB → RGB pre-corrections applied before the ICC profile converts to CMYK.
    // The ICC profile does the actual CMYK math; these entries just steer where
    // the gamut clip lands. See docs/2026-05-07-cmyk-pipeline.md.
    "#E74C3C": {
      "target": "#D14B3C",
      "label": "saturated red → print-safe red",
      "notes": "Pre-desaturating slightly avoids the muddy ICC clip."
    }
  },
  "matching": {
    "nearest_enabled": true,
    "metric": "lab",       // "lab" (CIE ΔE) or "rgb" (Euclidean)
    "threshold": 10.0      // max distance before falling back to "manual"
  },
  "print_safety": {
    "min_gray_value": "#EEEEEE",  // anything lighter triggers a warning
    "warn_only": true             // set false to make CLI exit non-zero
  },
  "logging": { "level": "INFO" }
}
```

Hex codes are normalized to uppercase 6-digit `#RRGGBB` internally; you can
write them in any case in the config.

## Running the Streamlit app

```powershell
.\launch_app.bat
```

or

```powershell
& .\.venv\Scripts\python.exe -m streamlit run app\app.py
```

The app has eleven sidebar destinations organised as: **Library** ·
**Project** (Semantic Palette + Accessibility, both cross-pipeline) ·
**Grayscale** pipeline (4 tabs) · **CMYK** pipeline (5 tabs).

1. **Library** — table of every SVG in `input/` with both grayscale and
   CMYK status columns. Multi-row selection drives the action bar:
   *Open* (single selection), *Wipe grayscale (N)*, *Wipe CMYK (N)*. The
   *Wipe ALL* expander below clears per-file config across the whole
   library for one pipeline at a time (gated by a confirm checkbox).
   Wipes clear per-file `overrides` + reset that pipeline's status to
   `pending`; the other pipeline and the global maps are left alone.
   Metadata files that end up empty for both pipelines are deleted so
   `metadata/` stays clean.
2. **Semantic Palette** *(Project)* — bind authored hexes to named slots
   (`status.bad`, `status.good`, …) and slots to per-pipeline targets
   inside a theme. Re-skinning for a different publisher = swap themes,
   no per-illustration touching. A one-shot **Migrate** button promotes
   existing global-map entries to auto-named slots you can rename at
   leisure. Resolution order at apply time: per-file override → active
   theme → legacy global map → pass-through, so adoption is incremental
   and additive.
3. **Accessibility** *(Project)* — color-blind preview across the whole
   library using Machado-2009 sRGB matrices. Two modes: a **library
   strip** (one row per illustration, columns = simulations) for a
   quick risk audit, and a **per-illustration grid** that lists which
   color pairs collapse under which CB type. Severity slider 0.5–1.0;
   "Show only affected" filter. Operates at the SVG level (color
   substitution + inline render) — fast across 20+ files.
4. **Editor** *(Grayscale)* — per-illustration editor. Side-by-side
   original vs. converted live preview, per-color picker with history
   suggestions, a per-row **↺ reset** that clears that color's per-file
   override and its global-map entry in one click, and an *Open output
   folder* shortcut. Save flows drop identity picks (`target == source`)
   and picks that already match the global map.
5. **Global Map** *(Grayscale)* — view and edit the grayscale global
   registry. Usage counts.
6. **Batch Export** *(Grayscale)* — writes `<name>_grayscale.svg` and
   (when enabled) `<name>_grayscale.png` at the configured DPI into
   `output/`.
7. **CMYK Editor** — per-illustration editor. Side-by-side original vs.
   RGB-corrected live preview, per-color picker for the CMYK correction
   map, "Generate CMYK soft-proof" button, and an **Apply auto-fixes**
   checkbox (persisted) that opts the file into Ghostscript's
   `-dBlackText -dBlackVector` force-K flags during the next batch.
8. **CMYK Global Map** — view and edit the project-wide
   `cmyk_correction_map`. Usage counts across illustrations.
9. **Palette** *(CMYK)* — curated CMYK swatch picker. Cluster every
   source color in the library into a small set of swatches via Lab
   k-means; each swatch shows its *printed appearance* (ICC roundtrip)
   plus a colored **ΔE76 badge** (green ≤ 2 / yellow 2–5 / red > 5).
   "Highlight ΔE ≥ 5" header toggle outlines gamut-clipping swatches in
   red. "Replace globally" updates the global correction map + cleans
   per-file overrides in one pass with a before / after / on-press
   visual diff. See [`docs/2026-05-08-curated-palette.md`](docs/2026-05-08-curated-palette.md).
10. **CMYK Print Export** — batch: writes `<name>_CMYK.pdf` (with the
    soft-proof PNG carrying trim / bleed / safety overlays) into
    `output_cmyk/`, plus an HTML QA report whose per-file row includes
    **TAC max %** and **force-K detection counts** with a tooltip
    showing mean / p99 / over-limit fraction. The bottom of the tab
    has a **Create delivery package** button that snapshots
    `config.json`, `color-config.json`, `semantic-palette.json` plus
    every PDF (with SHA-256) into `deliveries/<UTC-stamp>-<slug>/`.
11. **CMYK Settings** — paths, ICC profile, Ghostscript binary,
    trim/bleed, PDF/X-1a, soft-proof DPI, audit-sidecars toggle, plus
    the new **TAC limit / sample DPI / min stroke pt / min text pt**
    print-quality knobs, **soft-proof guides** toggle + safety-margin
    inset, and the **filename template** with a live preview against
    the first three SVGs. Also exposes the
    *Clean identity entries* maintenance button.

The grayscale **Settings** tab mirrors this: read-only summary at the
top, editable form for paths, PNG export, matching, and print safety
(persisted to `config.json` / `color-config.json` respectively), and a
matching *Clean identity entries from all grayscale metadata* button.

### Two-step grayscale-then-CMYK workflow (no app changes needed)

If you want CMYK PDFs that contain only black ink (pure-K), you can:

1. Run the grayscale pipeline as usual — `output/<name>_grayscale.svg`
   files contain only neutral grays.
2. Temporarily point `paths.input_dir` at `./output` in `config.json`,
   then run `cmyk-convert`. The ICC profile maps neutral RGB grays to
   pure K under typical profiles (e.g. ISO Coated v2 ECI). Profiles
   that simulate paper tone may add a faint chromatic tint — verify
   with `gswin64c -o nul -sDEVICE=inkcov` if it matters.
3. Restore `paths.input_dir` afterwards.

No `cmyk_correction_map` entries fire on grayscale inputs — the source
colors no longer match.

## CLI batch usage

```powershell
& .\.venv\Scripts\python.exe -m src.cli --help
```

### Grayscale pipeline

```powershell
# convert everything in input/ using current mappings
& .\.venv\Scripts\python.exe -m src.cli convert

# convert only illustrations marked reviewed
& .\.venv\Scripts\python.exe -m src.cli convert --only-reviewed

# inspect a single SVG without writing output
& .\.venv\Scripts\python.exe -m src.cli inspect input\figure-01.svg

# scan the library and print status
& .\.venv\Scripts\python.exe -m src.cli status
```

### CMYK pipeline

```powershell
# scan the library and print CMYK pipeline status
& .\.venv\Scripts\python.exe -m src.cli cmyk-status

# inspect one SVG: shows colors, dependency check, and (with --show-command)
# the exact Ghostscript command that would be run
& .\.venv\Scripts\python.exe -m src.cli cmyk-inspect input\figure-01.svg --show-command

# dry-run a batch — verifies all dependencies and prints the plan
& .\.venv\Scripts\python.exe -m src.cli cmyk-convert --dry-run

# convert everything in input/ to CMYK PDFs in output_cmyk/
& .\.venv\Scripts\python.exe -m src.cli cmyk-convert

# convert only illustrations marked CMYK-reviewed
& .\.venv\Scripts\python.exe -m src.cli cmyk-convert --only-reviewed

# override the output filename template just for this run
& .\.venv\Scripts\python.exe -m src.cli cmyk-convert `
    --filename-template "fig_{chapter:02d}_{figure:02d}_CMYK"
```

The CMYK CLI emits per-file status, a final summary, and writes
`output_cmyk/cmyk_qa_report.html` describing the run (now with TAC and
force-K columns per file).

### Delivery snapshots

```powershell
# snapshot project state + every PDF in output_cmyk/ to deliveries/
& .\.venv\Scripts\python.exe -m src.cli deliver --label "acme-2026-05"
```

Bundles `config.json`, `color-config.json`, `semantic-palette.json` (when
present), every matching PDF + soft-proof PNG (hardlinked when possible),
a `manifest.json` with SHA-256 per file, and an auto-generated
`README.md`. One snapshot per publisher hand-off so tweaks weeks later
are byte-reproducible.

## CMYK print export workflow

Used independently of (or alongside) the grayscale pipeline when delivering
color CMYK PDFs to a publisher.

1. **Install dependencies** (one-time): Ghostscript on PATH, ICC profile in
   `profiles/`. Confirm with
   `& .\.venv\Scripts\python.exe -m src.cli cmyk-inspect input\<file>.svg`.
2. **Set trim size** in the CMYK Print Export tab (or `cmyk_export.target_*`
   in `config.json`). Default is 5.5″ × 7.5″ no bleed; ask your publisher.
3. **Seed the correction map.** Open the CMYK Global Map tab. The
   `color-config.json.example` ships a starter set of common
   gamut-safe corrections (saturated red → less saturated, pure black →
   near-black, etc.). Copy what fits your palette.
4. **Converge on a shared palette.** Open the **Palette** tab. Click
   *Generate* in the seed panel to cluster every color in your library
   into a small set of swatches whose tiles show the *printed*
   appearance via an ICC roundtrip. Pick swatches that look right on
   press, click **Replace globally**, and confirm — the global
   correction map gets the new entry and every per-file override of
   the swatch's members is cleaned up in one pass. The before / after
   / on-press visual diff in the confirm dialog shows you exactly
   which illustrations will change before you commit.
5. **Iterate per illustration if needed.** For finer per-file
   adjustments: open the **CMYK Editor** tab, eyeball the live
   RGB-corrected preview, click **Generate CMYK soft-proof** to see
   the press-side result. The `↺ reset` button on each row clears
   that color's per-file override AND its global correction-map entry
   in one click — the color then passes through to ICC unchanged.
6. **Mark reviewed.** Save & mark reviewed in the CMYK Editor.
7. **Batch export.** Run the **CMYK Print Export** tab (or
   `python -m src.cli cmyk-convert --only-reviewed`). Output lands in
   `output_cmyk/<name>_CMYK.pdf` plus a soft-proof PNG and an HTML QA
   report.
8. **Verify** (optional but recommended):
   ```powershell
   gswin64c -o nul -sDEVICE=inkcov output_cmyk\<file>_CMYK.pdf
   pdfinfo output_cmyk\<file>_CMYK.pdf
   ```
   `inkcov` shows per-page CMYK ink coverage — a real four-color PDF will
   print four numbers, not one.

For the *why* behind the design (RGB correction vs explicit CMYK overrides,
soft-proof timing, ICC profile choice, PDF/X), see
[`docs/2026-05-07-cmyk-pipeline.md`](docs/2026-05-07-cmyk-pipeline.md). For
the theory behind the publisher-grade additions (TAC, force-K, semantic
palette, color-blind risk, delivery snapshots), see
[`docs/2026-05-09-publisher-grade-additions.md`](docs/2026-05-09-publisher-grade-additions.md).

## End-to-end workflow

1. Author the illustration in Affinity Designer 2 with the canonical color
   palette (any reds you mean as "bad", any greens you mean as "good", etc.).
2. **File → Export → SVG** into this project's `input/` folder.
3. Open the Streamlit app, go to **Library**, click the new file.
4. In **Editor**, review the auto-suggested mappings (exact-hex first, then
   nearest within the configured threshold). Tweak any that look wrong using
   the color picker. Watch the live side-by-side preview.
5. **Save & mark reviewed.** The mapping is recorded in
   `metadata/<file>.mapping.json` and any *new* exact mappings you confirmed
   are also promoted to the global map.
6. Repeat for the rest of the library.
7. When everything is reviewed, run **Batch Export** (or `python -m src.cli
   convert --only-reviewed`). Each illustration produces `<name>_grayscale.svg`
   and, when PNG export is enabled, `<name>_grayscale.png` in `output/`.
8. Re-open `_grayscale.svg` files in Affinity Designer 2 for any final touch-ups,
   then place into the book layout. Use the `_grayscale.png` files for raster-only
   contexts (Word, presentations, web).

## Testing

```powershell
& .\.venv\Scripts\python.exe -m pytest
```

Unit tests cover `svg_parser`, `color_mapper`, and `mapping_store` — including
the tricky cases: `<style>` blocks, named colors, `rgb(...)`, 3-digit
shorthand, malformed input, and nearest-color edge cases.

## SVG coverage

The parser/writer round-trips:

- `fill="#..."` and `stroke="#..."` inline attributes
- `<stop stop-color="..."/>` and `stop-color` inside CSS
- Hex colors inside `<style>` blocks (CSS rules)
- `style="fill: ...; stroke: ..."` inline CSS
- Named colors (`red`, `cornflowerblue`, ...) — normalized to hex
- `rgb(r, g, b)` notation — normalized to hex
- 3-digit shorthand (`#F00`) — expanded to `#FF0000`

Everything else (paths, transforms, text, IDs, metadata, comments) is preserved
byte-for-byte where possible so the round-trip back into Affinity is clean.

## Inkscape usage

Inkscape serves two roles in this project:

**PNG export** (automatic) — the Batch Export tab calls Inkscape via CLI to
rasterise each `_grayscale.svg` to a `_grayscale.png` at the configured DPI.
Configure the path via `png_export.inkscape_path` in `config.json`.

**SVG pre-normalisation** (manual, if needed) — if a particular SVG hits an edge
case the in-house parser doesn't handle cleanly (exotic CSS, unusual structure),
pre-normalise it through Inkscape before importing:

```powershell
& "C:\Program Files\Inkscape\bin\inkscape.exe" --export-plain-svg `
    --export-filename=tmp\figure-01.normalized.svg input\figure-01.svg
```

Affinity Designer 2 produces clean SVG in practice, so this is rarely needed.
