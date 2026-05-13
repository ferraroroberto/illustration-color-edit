# CMYK Print Export Pipeline — Design Notes

**Date:** 2026-05-07
**Author:** notes captured during the design + implementation session
**Scope:** new feature alongside the existing grayscale workflow

---

## Why this exists

The book illustrations carry semantic color (red = bad, green = good). The
grayscale pipeline already handles the print-grayscale edition for the body
of the book. Separately, the publisher wants a **color CMYK** delivery for a
set of ~30 illustrations at specific trim dimensions (5.5″ × 7.5″ default).

Doing this manually per illustration is not viable. We need a one-shot batch:
take a folder of RGB SVGs (Affinity Designer 2 exports), produce
press-ready CMYK PDFs at the requested page size, with a one-time palette
calibration to compensate for the RGB→CMYK gamut shift.

## The CMYK problem in three sentences

When an RGB SVG (e.g. a vivid `#E74C3C` red) is converted to CMYK for press,
three things shift:

1. **Out-of-gamut clip.** Saturated reds, oranges, vivid greens and pure
   blues lie outside the CMYK gamut. The ICC conversion clips them, often
   producing a muddy result that no longer matches what the author saw.
2. **Rich black.** Pure RGB `#000000` typically converts to a CMY+K
   "rich black" (e.g. 50/40/40/100) that can mis-register on press. Many
   publishers want pure K instead.
3. **Muddy greys.** RGB greys built from equal R=G=B convert to 4-color
   CMYK greys instead of clean K-only.

These are well-known and inevitable; the question is how the *pipeline*
gives the user control over the result.

## The approach we took

Two stages, with a deliberate split of responsibilities:

```
SVG (RGB)
  └─► [1] RGB→RGB pre-correction  ──►  SVG (RGB, corrected)
                                          │
                                          ▼
                               [2] Inkscape: SVG → PDF (RGB)
                                          │
                                          ▼
                               [3] Ghostscript + ICC: RGB PDF → CMYK PDF
```

* **Stage 1** is *under the user's control*. We expose a global
  `cmyk_correction_map` (parallel to the grayscale `global_color_map`),
  with optional per-illustration `cmyk_overrides`. This lets the user nudge
  problem colors into a print-safe RGB starting point before they ever
  reach the ICC profile.
* **Stages 2 + 3** are deterministic. Once a starting RGB is chosen,
  Inkscape rasterises it into a PDF page at the right dimensions, and
  Ghostscript runs the ICC math. There's no per-color knob here — the
  knobs live in stage 1.

The mental model: *the ICC profile does the math; the correction map steers
where it lands.*

### Why this beats explicit DeviceCMYK overrides

A "more honest" approach would let the user write `(C, M, Y, K)` values per
source color, bypassing the ICC profile entirely. We considered it and
rejected it for v1:

* **SVG can't carry DeviceCMYK natively.** The SVG color spec is RGB.
  Implementing real CMYK overrides requires post-processing the rendered
  PDF to inject DeviceCMYK colors, which means parsing PDF content streams.
  That's a 2–3× the implementation surface of the current approach.
* **Empirical iteration is cheaper than theoretical CMYK math.** Soft-proof
  PNGs let the user *see* the result of a correction in seconds. Tweak the
  RGB pre-correction → re-soft-proof → done. Predicting CMYK values up
  front requires deeper color theory than the day-to-day workflow needs.
* **Fallback exists.** If a particular color absolutely won't behave with
  pre-correction, the user can always do that one color in Affinity
  manually and re-export. The pipeline isn't load-bearing for every pixel.

If proofs from the press come back showing pre-correction is insufficient,
we can add explicit DeviceCMYK overrides as a follow-up — see "Future
work" below.

## Two previews, by design

The CMYK Editor tab has **two** previews:

1. **Real-time RGB-corrected preview** (always on). The SVG is re-rendered
   in the browser with the current correction map applied. This is what
   feeds into the ICC conversion. Updates as you tweak any color.
2. **On-demand CMYK soft-proof** (button). Runs the full Inkscape →
   Ghostscript pipeline once, renders the resulting CMYK PDF back to a PNG
   *through* the ICC profile, and shows it inline. Cached until the
   correction map changes.

We can't make the soft-proof real-time — it's a multi-process pipeline
that takes a few seconds. But we can make it cheap to ask for, and that's
what the "Generate soft-proof" button does. The cache invalidates on map
change so you never look at a stale proof by accident.

## Tool choices

### Inkscape over cairosvg for SVG → PDF
Inkscape is already a project dependency (used for grayscale PNG export).
On Affinity Designer SVGs in this project, Inkscape consistently produces
clean PDFs with gradients, embedded rasters, and live effects intact;
cairosvg has historically lost ground here. No new system dependency, no
new Python package — keep `requirements.txt` lean.

### Ghostscript for RGB PDF → CMYK PDF
Ghostscript is the canonical free tool for ICC-driven color space
conversion in PDFs. It honors the ICC profile, supports PDF/X-1a output,
and produces output that printers and pre-press tools accept. Alternatives
(qpdf, mutool) don't ship the same color-management depth.

### littleCMS / Pillow for embedded rasters?
Out of scope for v1. Embedded `<image>` rasters pass through Ghostscript's
ICC conversion, which gets you most of the way. If a particular bitmap
needs precise CMYK, do it in Affinity / Photoshop separately.

## ICC profile guidance

Always **ask the publisher first** which profile they want. If they have no
preference, sensible free defaults:

* **ISO Coated v2 (ECI)** — European offset standard; good default for EU
  publishers. Free download: https://www.eci.org → Downloads → ICC Profiles.
* **U.S. Web Coated (SWOP) v2** — US standard. Ships with Adobe products,
  also at color.org.
* **FOGRA39** — German/EU offset, very common for trade book printing.

Drop the `.icc` file into `profiles/` and point
`cmyk_export.icc_profile_path` at it.

## PDF/X-1a:2003

`cmyk_export.pdfx_compliance: true` makes Ghostscript emit a PDF/X-1a:2003
file:

* Forces all colors to be DeviceCMYK or spot.
* Forbids transparency. **This is the gotcha:** if your SVG uses
  semi-transparent fills, Ghostscript will warn and the result may not be
  strictly PDF/X compliant. Flatten transparency in Affinity before export
  if your publisher requires PDF/X.
* Embeds an Output Intent referring to the ICC profile.

For most trade publishers, plain DeviceCMYK PDF is acceptable; turn PDF/X
on only if asked.

### Implementation notes (Ghostscript 10.x)

`-dPDFX=true` alone does **not** produce a PDF/X file — it only enables
extra checks. The actual `/GTS_PDFXVersion`, `/Trapped`, and
`/OutputIntents` markers come from a `PDFX_def.ps` file that Ghostscript
runs as a positional PostScript argument. The pipeline auto-generates one
per output (`<name>.pdfx_def.ps` next to the CMYK PDF) using
`write_pdfx_def_ps()` in `src/cmyk_convert.py`. The generated file
embeds the ICC profile via `(<icc-path>) (r) file` and declares
`/GTS_PDFXVersion (PDF/X-1:2001)` (PDF/X-1a:2003 extends that base spec).

Two GS 10.x quirks the def-file path triggered:

* **`/undefinedfilename in (.4)`** — passing `-dCompatibilityLevel=1.4`
  alongside `-dPDFX=true` causes the value parser to leak `".4"` onto
  the operand stack, which PostScript later tries to `run` as a file.
  Fix: do **not** set `-dCompatibilityLevel=1.4`; GS picks the right
  version automatically when `-dPDFX` is on (PDF 1.3 for PDF/X-1).
* **`/invalidfileaccess` / Permission denied** — `-dSAFER` (default in
  GS 10.x) blocks the def file's `(...) (r) file` operator. Fix:
  whitelist the ICC profile with `--permit-file-read=<path>`. The
  pipeline does this automatically.

The `OutputConditionIdentifier` is derived from the ICC filename
(`USWebCoatedSWOP.icc` → `CGATS TR 001`, `CoatedGRACoL2006.icc` →
`CGATS TR 006`, `ISOcoated_v2.icc` → `FOGRA39L`, fallback `Custom`).

## Edge cases & warnings the pipeline surfaces

The pipeline scans each SVG and surfaces these in the QA report:

| Detected         | Why it matters                                      | Fix                                            |
|------------------|-----------------------------------------------------|------------------------------------------------|
| `<image>` element | Embedded rasters pass through ICC as-is             | For best fidelity, CMYK-convert rasters first  |
| `<text>` element  | Font embedding / publisher rejects unfontable text  | Affinity → Layer → Convert to Curves           |
| Live effects     | Affinity sometimes rasterises filters to PNG inside SVG | Flatten effects in Affinity before export  |
| Gradients with many stops | Usually fine; just verify in soft-proof   | Visual check                                   |
| Transparency + PDF/X | PDF/X-1a forbids transparency                    | Flatten in Affinity, or disable PDF/X          |

## How to verify a CMYK PDF

After a batch run:

```powershell
# 1. Confirm the output is DeviceCMYK
gswin64c -o nul -sDEVICE=inkcov path\to\output_CMYK.pdf
# Outputs per-page ink coverage as four numbers (C M Y K) — proves it's
# really four-color.

# 2. Inspect metadata
pdfinfo path\to\output_CMYK.pdf
# Look for: Page size matches your trim+bleed; PDF version 1.4 or 1.6.

# 3. Eyeball the soft-proof PNG next to the original SVG.
```

## Architecture: how it slots into the existing project

The CMYK pipeline mirrors the grayscale pipeline structurally:

| Concern                     | Grayscale                       | CMYK                              |
|-----------------------------|---------------------------------|-----------------------------------|
| Per-illustration metadata   | `status` + `overrides`          | `cmyk_status` + `cmyk_overrides`  |
| Project-wide map            | `global_color_map`              | `cmyk_correction_map`             |
| Editor tab                  | `tab_editor.py`                 | `tab_cmyk_editor.py`              |
| Global map tab              | `tab_global_map.py`             | `tab_cmyk_global_map.py`          |
| Batch tab                   | `tab_batch.py`                  | `tab_cmyk_export.py`              |
| CLI subcommands             | `status` / `inspect` / `convert`| `cmyk-status` / `cmyk-inspect` / `cmyk-convert` |
| Output filename suffix      | `<name>_grayscale.svg/.png`     | `<name>_CMYK.pdf` + `<name>_CMYK_preview.png` |
| Status tracking             | independent per pipeline        | independent per pipeline          |

The same SVG can be `reviewed` for grayscale and `pending` for CMYK. The
two pipelines never write to each other's fields.

## Audit sidecars (per-file report + PDF/X def file)

The pipeline can drop two companion files next to each `<name>_CMYK.pdf` so
a book editor or prepress operator can see exactly how a given illustration
was produced:

* `<name>_CMYK_report.txt` — plain UTF-8 text. Records the ICC profile (path,
  size, OutputCondition), Ghostscript version + resolved binary path, the
  full GS command that ran, page geometry (trim/bleed/MediaBox), the count
  of RGB→RGB color replacements applied during pre-correction, any unmapped
  colors, SVG content warnings (embedded raster `<image>` / un-outlined
  `<text>`), and elapsed time.
* `<name>_CMYK.pdfx_def.ps` — only when `pdfx_compliance` is on. The
  PostScript definition file Ghostscript runs *before* `pdfwrite` to inject
  the OutputIntent / `/GTS_PDFXVersion` markers into the catalog. It's
  required during conversion either way; this setting just controls whether
  it's kept on disk afterwards.

Controlled by **`cmyk_export.audit_artifacts`** in `config.json` (default
`true`). Editable from the CMYK Settings tab. When `false`, only the final
PDF (and the optional `_preview.png`) survive — and on every run any prior
sidecars for the same stem are removed first, so toggling the setting
between runs never leaves orphans behind.

The Ghostscript version is probed once per batch (`gs -v`) and reused for
every per-file report. Soft-proofs from the CMYK Editor never write a
report — they're scratch previews.

## Future work

Items deferred from v1 have been migrated to GitHub issues:

* Explicit DeviceCMYK overrides + PDF/X-4 support → see the "Advanced press-color control" issue.
* Multi-folder batch UI, CLI `cmyk-soft-proof`, and automatic ICC profile download → see the "Workflow ergonomics" issue.
