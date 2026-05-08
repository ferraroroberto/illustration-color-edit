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

## Project structure

```
illustration-color-edit/
├── app/
│   ├── app.py                  # Streamlit entry point (scaffolding + tab routing)
│   ├── common.py               # shared Streamlit helpers (swatches, badges, caching)
│   ├── tab_library.py          # Library tab
│   ├── tab_editor.py           # Editor tab (grayscale)
│   ├── tab_global_map.py       # Global Map tab (grayscale)
│   ├── tab_batch.py            # Batch Export tab (grayscale)
│   ├── tab_cmyk_editor.py      # CMYK Editor tab (per-illustration corrections + soft-proof)
│   ├── tab_cmyk_global_map.py  # CMYK correction map tab
│   ├── tab_cmyk_export.py      # CMYK Print Export tab (batch SVG → CMYK PDF)
│   └── tab_settings.py         # Settings tab
├── src/
│   ├── svg_parser.py       # extract colors, parse <style> blocks, normalize
│   ├── color_mapper.py     # exact + nearest-color matching, suggestion engine
│   ├── svg_writer.py       # apply mappings, write output SVG
│   ├── library_manager.py  # scan input dir, track per-file status
│   ├── mapping_store.py    # global + per-illustration mapping persistence (both pipelines)
│   ├── print_safety.py     # gray-value warnings for uncoated paper
│   ├── svg_to_pdf.py       # Inkscape wrapper: SVG → RGB PDF at trim size
│   ├── cmyk_convert.py     # Ghostscript wrapper: RGB PDF → CMYK PDF via ICC
│   ├── cmyk_pipeline.py    # CMYK pipeline orchestrator (single + batch + soft-proof)
│   ├── qa_report.py        # CMYK batch HTML QA report writer
│   └── cli.py              # batch CLI entry point (both pipelines)
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
    "preview_dpi": 150                                      // soft-proof resolution
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

The app has eight horizontal tabs, organised as: Library | grayscale-pipeline
trio | CMYK-pipeline trio | Settings.

1. **Library** — list all SVGs in `input/` with grayscale status badges
   (`pending` / `in_progress` / `reviewed` / `exported`). Click to open.
2. **Editor** — grayscale per-illustration editor. Side-by-side original vs.
   converted live preview, per-color picker with history suggestions,
   save / mark-reviewed.
3. **Global Map** — view and edit the grayscale global registry. Usage counts.
4. **Batch Export** — batch grayscale: writes `<name>_grayscale.svg` and (when
   enabled) `<name>_grayscale.png` at the configured DPI into `output/`.
5. **CMYK Editor** — CMYK per-illustration editor. Side-by-side original vs.
   RGB-corrected live preview, per-color picker for the CMYK correction map,
   plus a "Generate CMYK soft-proof" button that runs the full Inkscape →
   Ghostscript pipeline once and shows the resulting PNG inline.
6. **CMYK Global Map** — view and edit the project-wide
   `cmyk_correction_map`. Usage counts across illustrations.
7. **CMYK Print Export** — batch CMYK: writes `<name>_CMYK.pdf` (and a
   `<name>_CMYK_preview.png` soft-proof when enabled) at the configured trim
   size into `output_cmyk/`, plus an HTML QA report.
8. **Settings** — paths, matching threshold, print-safety threshold, PNG export
   settings.

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
```

The CMYK CLI emits per-file status, a final summary, and writes
`output_cmyk/cmyk_qa_report.html` describing the run.

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
4. **Iterate per illustration.** For each file: open the **CMYK Editor**
   tab, eyeball the live RGB-corrected preview, click **Generate CMYK
   soft-proof** to see the press-side result. Tweak any problem colors and
   re-soft-proof until you like it.
5. **Mark reviewed.** Save & mark reviewed in the CMYK Editor.
6. **Batch export.** Run the **CMYK Print Export** tab (or
   `python -m src.cli cmyk-convert --only-reviewed`). Output lands in
   `output_cmyk/<name>_CMYK.pdf` plus a soft-proof PNG and an HTML QA
   report.
7. **Verify** (optional but recommended):
   ```powershell
   gswin64c -o nul -sDEVICE=inkcov output_cmyk\<file>_CMYK.pdf
   pdfinfo output_cmyk\<file>_CMYK.pdf
   ```
   `inkcov` shows per-page CMYK ink coverage — a real four-color PDF will
   print four numbers, not one.

For the *why* behind the design (RGB correction vs explicit CMYK overrides,
soft-proof timing, ICC profile choice, PDF/X), see
[`docs/2026-05-07-cmyk-pipeline.md`](docs/2026-05-07-cmyk-pipeline.md).

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
