# Illustration Color Edit

Industrial-grade SVG color-to-grayscale conversion pipeline for book illustrations
where color carries semantic meaning (red = bad, green = good, etc.).

The tool consists of:

- A **Streamlit app** (`app/app.py`) for interactive per-illustration mapping with
  side-by-side preview and suggestion engine.
- A **CLI batch processor** (`src/cli.py`) for converting an entire library once
  mappings are locked in.

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
│   ├── app.py              # Streamlit entry point (scaffolding + tab routing)
│   ├── common.py           # shared Streamlit helpers (swatches, badges, caching)
│   ├── tab_library.py      # Library tab
│   ├── tab_editor.py       # Editor tab (side-by-side preview, suggestions)
│   ├── tab_global_map.py   # Global Map tab
│   ├── tab_batch.py        # Batch Export tab
│   └── tab_settings.py     # Settings tab
├── src/
│   ├── svg_parser.py       # extract colors, parse <style> blocks, normalize
│   ├── color_mapper.py     # exact + nearest-color matching, suggestion engine
│   ├── svg_writer.py       # apply mappings, write output SVG
│   ├── library_manager.py  # scan input dir, track per-file status
│   ├── mapping_store.py    # global + per-illustration mapping persistence
│   ├── print_safety.py     # gray-value warnings for uncoated paper
│   └── cli.py              # batch CLI entry point
├── tests/                      # pytest unit tests
├── tmp/                        # working scratch space (gitignored except .gitkeep)
├── input/                      # source SVGs — gitignored, path set in config.json
├── output/                     # converted SVGs — gitignored, path set in config.json
├── metadata/                   # per-illustration .mapping.json — gitignored, path set in config.json
├── config.example.json         # committed template for folder paths
├── config.json                 # gitignored, your local folder paths
├── color-config.json.example   # committed template for color settings
├── color-config.json           # gitignored, your local color mappings
├── .env.example
├── .gitignore
├── requirements.txt
├── launch_app.bat
└── README.md
```

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
    "output_dir": "./output",  // converted grayscale SVGs
    "metadata_dir": "./metadata"  // per-illustration mapping overrides
  }
}
```

### `color-config.json` — color mappings and parameters

```jsonc
{
  "global_color_map": {
    "#E74C3C": { "target": "#333333", "label": "red / bad", "notes": "..." }
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

The app has five horizontal tabs:

1. **Library** — list all SVGs in `input/` with status badges
   (`pending` / `in_progress` / `reviewed` / `exported`). Click to open.
2. **Editor** — side-by-side original vs. converted preview. Per-source-color
   pickers with suggestions ("this red was previously mapped to #333333 in 4
   other illustrations — reuse?"). Save / mark-reviewed.
3. **Global Map** — view and edit the global registry. Shows usage counts.
4. **Batch Export** — convert every reviewed (or every) illustration in `input/`
   using current mappings. Progress + summary report.
5. **Settings** — paths, matching threshold, print-safety threshold.

## CLI batch usage

```powershell
& .\.venv\Scripts\python.exe -m src.cli --help

# convert everything in input/ using current mappings
& .\.venv\Scripts\python.exe -m src.cli convert

# convert only illustrations marked reviewed
& .\.venv\Scripts\python.exe -m src.cli convert --only-reviewed

# inspect a single SVG without writing output
& .\.venv\Scripts\python.exe -m src.cli inspect input\figure-01.svg

# scan the library and print status
& .\.venv\Scripts\python.exe -m src.cli status
```

The CLI prints a final report: files processed, files skipped, unmapped colors
encountered (per file), and print-safety warnings.

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
   convert --only-reviewed`). Output SVGs land in `output/`.
8. Re-open output SVGs in Affinity Designer 2 for any final touch-ups, then
   place into the book layout.

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

## Optional Inkscape fallback

If a particular SVG hits an edge case the parser doesn't handle cleanly (e.g.
exotic CSS the in-house parser misses), pre-normalize it through Inkscape:

```powershell
& "C:\Program Files\Inkscape\bin\inkscape.exe" --export-plain-svg `
    --export-filename=tmp\figure-01.normalized.svg input\figure-01.svg
```

then point the app at `tmp\figure-01.normalized.svg`. There's no automatic
hand-off — Affinity Designer 2 itself produces clean SVG in practice.
