# Publisher-grade additions — Design + Theory Notes

**Date:** 2026-05-09
**Scope:** six-phase expansion turning the per-illustrator pipeline into something
a publisher's prepress team will accept without rework. Each section pairs the
*what* (the feature) with the *why* (the press-side theory it addresses).

---

## Why this exists

The pre-existing pipeline did the right things at the *file* level — per-
illustration mapping, ICC roundtrip preview, audit sidecars — but was
missing the checks and abstractions that publishers' prepress departments
routinely flag. The features below close that gap. They're additive: an
existing project keeps working, and the new checks default to **warn-only**
so nothing changes silently.

The user (a working illustrator preparing books for publishers) approved
four architectural choices up front:

1. **Single-publisher** for now, with data shapes that allow a later
   migration to per-publisher profiles without a rewrite.
2. **Filename templating** auto-parses `<chapter>.<figure>` from source
   filenames; no separate config file.
3. **Color-blind preview** lives in a dedicated **Accessibility** tab and
   runs only on RGB + grayscale (skipping the CMYK soft-proof, which is
   too expensive to render per illustration per CB type).
4. **TAC + force-K** are warn-only by default with a per-illustration
   opt-in to apply auto-fixes.

---

## Phase 1 — Cheap wins

### 1a. ΔE per swatch in Palette tab

**Theory.** When you push a saturated sRGB color through a CMYK ICC
profile, the gamut clip lands somewhere — and the question that matters
for press is *how far*. CIE76 ΔE is a Lab-space distance: ΔE < 1 is not
perceptible, 2–5 is "you can see it if you look", and >5 is "obviously a
different color". The Palette tab already had the printed-appearance
swatch (the result of the sRGB→CMYK→sRGB roundtrip via the active ICC).
Now it also shows the numeric ΔE76 between source and printed appearance
as a colored badge (green / yellow / red). A header checkbox highlights
swatches with ΔE ≥ 5 in red so out-of-gamut picks are visible at a
glance without hovering.

**Implementation.** No new math — `cmyk_gamut.cmyk_gamut_delta` already
computed ΔE76. Added a `numeric_metric_cell()` helper to `app/common.py`
and used it in `tab_palette.py`'s detail panel + grid hover tooltip.

### 1b. sRGB assertion at parse time

**Theory.** The whole pipeline assumes sRGB input. Affinity Designer 2
honours that by default and emits SVGs without any color-space
metadata. But an SVG that was edited in Inkscape and tagged
`color-interpolation="linearRGB"`, or that contains a `<color-profile>`
element, is silently outside the assumption. The ICC math then renders
"correctly" but to the wrong destination, and the printed result drifts
from the soft-proof.

**Implementation.** `parse_svg()` now scans for `<color-profile>`
elements and `color-interpolation` / `color-interpolation-filters`
attributes whose values aren't sRGB-equivalent. Findings land in
`ParsedSVG.color_space_warnings` and surface as `st.warning(...)` rows
in both editor tabs and as `! …` lines in `cli inspect` /
`cli cmyk-inspect`. It's a guardrail, not a hard fail — the pipeline
still runs.

### 1c. Filename templating

**Theory.** Illustrators name source files by intent (`04.03 - venn
diagram two - myth reality...svg`); publishers want delivery-ready
names (`fig_04_03_CMYK.pdf` or `<isbn>_chXX_figYY.pdf`). Manual
renames after every batch are error-prone and break reproducibility.

**Implementation.** `src/filename_template.py`:

* `parse_chapter_figure(stem)` — regex `^(\d+)[.\-_](\d+)\b` tolerates
  `04.03`, `1.2`, `4-3`, `4_3` (the user explicitly wanted multiple
  conventions, no separate config file).
* `apply_template(template, stem)` — supports `{stem}`, `{chapter}`,
  `{figure}` (raw or `:02d`-padded), `{description}` (stem with prefix
  stripped), `{slug}` (lowercased ASCII).

Wired into `CmykContext.filename_template`, the
`cmyk_export.filename_template` config field, a `--filename-template`
CLI flag, and a live preview in the CMYK Settings tab that renders the
first three SVGs through the template so typos surface before batch
time.

---

## Phase 2 — CMYK quality gates (TAC + force-K)

### 2a. Total Area Coverage

**Theory.** Offset presses can only deposit so much ink before the
substrate refuses to dry, the trapping fails, or the sheet cockles.
The cap is the *Total Area Coverage*: the per-pixel sum of C+M+Y+K,
each as a percentage. Published limits:

| Stock | Typical TAC limit |
|-------|-------------------|
| Coated (gloss/matte, ISO Coated v2 / SWOP / GRACoL territory) | 300–340% |
| Uncoated (offset book paper) | 240–280% |
| Newsprint | 220–240% |

A naive deep red can land at C 70 / M 100 / Y 100 / K 30 = 300% — at
the limit on coated, over the limit on uncoated. After ICC conversion
even a "harmless" looking #00008B navy can sit at 360%. Prepress will
reject the file.

**Implementation.** `src/cmyk_tac.py` rasterizes the produced CMYK PDF
to a 4-channel TIFF via Ghostscript's `tiff32nc` device (one byte per
channel, packed), loads it with PIL → numpy, sums channels per pixel,
and computes:

* `max_pct` — the worst single pixel, in percent (0–400).
* `mean_pct` — area-weighted mean.
* `p99_pct` — robust max, ignores one-pixel anti-alias artifacts.
* `violation_fraction` — fraction of pixels at or above the threshold.
* `status` — `ok` / `warn` (<0.1% over) / `fail`.

100 dpi is plenty for flat-color illustrations; configurable to
150–200 if features are very fine. Threshold defaults to 320%.

### 2b. Force-K (fine lines / small text on the K plate)

**Theory.** Four-color black is a CMYK mix that *adds up* to a black
appearance — usually around C 75 / M 68 / Y 67 / K 90 (= "rich black").
For large solids this looks great. For thin lines and small text, it's
a disaster: the four printing plates are physically separate, and any
mechanical misregistration of the press (typically 0.05–0.1 mm) leaves
a colored fringe along every edge. Hairlines disappear into a halo.
Small text becomes uncomfortable to read.

The fix is simple: render exact-black text and vectors on the **K plate
only** (C 0 / M 0 / Y 0 / K 100). Misregistration on a single plate is
invisible — the line just shifts, no halo.

**Implementation.** Two layers, both opt-in per file via
`IllustrationMapping.cmyk_auto_fix`:

* **Detection** (always runs). `src/force_k.py:find_fine_lines()`
  walks the SVG, computes stroke widths and font sizes in points at
  trim scale (using `viewBox` width vs trim inches), and counts
  near-black strokes ≤ 0.5 pt and near-black text ≤ 9 pt. Result lands
  in the audit sidecar and the QA report.
* **Application** (when `cmyk_auto_fix` is on). Adds Ghostscript's
  `-dBlackText=true -dBlackVector=true` to the conversion command,
  which forces exact-black text/vectors to K-only. The sentinel
  substitution path was deferred — Ghostscript's flags do the right
  thing for the Inkscape→GS pipeline shape, and the rewrite was
  flagged as risky in the original plan.

"Near black" is defined as ΔE76 ≤ 15 from #000000 (covers #1A1A1A and
slightly darker — colors a designer "meant" as black even if not
exactly #000000).

---

## Phase 3 — Bleed / safety overlay

**Theory.** Every printed page has three concentric rectangles:

* **Trim** — where the paper is cut. Always cut with some slop (~1 mm
  per side typical).
* **Bleed** — *outside* trim by 3–5 mm. Background art that should
  reach the page edge must extend to the bleed line, otherwise the
  cutter's slop will show as a white sliver.
* **Safety / live area** — *inside* trim by 4–5 mm. Anything you can't
  afford to lose (text, key annotations) belongs inside this.

A soft-proof PNG that doesn't show these is a partial answer to "is
this print-ready?".

**Implementation.** `src/bleed_overlay.py:composite_guides()` opens the
soft-proof PNG with PIL, draws a solid red trim rectangle, dashed
magenta bleed (when `bleed_in > 0`), and dashed cyan safety inset.
Configurable via `cmyk_export.safety_inches` (default 0.1875" ≈ 4.76 mm)
and `cmyk_export.show_guide_overlay` (default on). Wired into
`cmyk_pipeline.process_one` after `pdf_to_preview_png`.

---

## Phase 4 — Semantic palette layer

**Theory.** The original pipeline mapped at the hex level: `#E74C3C →
#373737`. That's two things conflated: the *intent* of the color
("status.bad — this represents something negative") and its *
presentation* (the hex used for grayscale or CMYK output). Tomorrow's
publisher wants reds slightly less aggressive, or a different paper
stock needs a softer dark, and you have to re-touch every illustration.

The standard remedy in design systems is a *named slot* layer between
intent and presentation:

```
authored hex          slot              theme[active][pipeline]
#E74C3C       →   status.bad   →   { cmyk: #D14B3C, grayscale: #373737 }
#46AA3A       →   status.good  →   { cmyk: #3FA037, grayscale: #E4E4E4 }
```

The illustrator paints with the authored hex. Slots are bound to
authored hexes once. Themes provide per-pipeline targets. Re-skinning
for a different publisher = swap themes; no per-illustration touching.

**Implementation.** `src/semantic_palette.py` stores
`semantic-palette.json` at the project root. Resolution order at apply
time, both pipelines:

1. Per-file override (`overrides` / `cmyk_overrides`).
2. Active theme: `source_hex` → owning slot → `theme[pipeline][slot]`.
3. Existing global map (legacy fallback).
4. Pass-through.

`merge_with_semantic()` enforces this order; every `merge_mappings()`
call site in the app + CLI was migrated. Existing `global_color_map` /
`cmyk_correction_map` entries continue to work for any color a slot
doesn't claim, so adoption is incremental and additive.

The new `app/tab_semantic_palette.py` provides slot CRUD, an active-
theme picker, and a one-shot **Migrate** button that walks the
existing global maps and creates auto-named slots (`auto.001`, `auto.002`,
…) the user can rename at leisure. `auto_migrate_global_map()` is
idempotent — rerunning is safe.

---

## Phase 5 — Accessibility (color-blind) tab

**Theory.** Roughly 8% of men have a red-green deficiency — far too
many to ignore in a book whose illustrations encode meaning in color
("red = bad, green = good"). The classic categories:

| Type | Affected | Approximate prevalence |
|------|----------|------------------------|
| Deuteranopia | M-cone defect (green-weak) | ~6% male / 0.4% female |
| Protanopia   | L-cone defect (red-weak)   | ~2% male / 0.01% female |
| Tritanopia   | S-cone defect (blue-weak)  | ~0.01% (rare) |
| Achromatopsia | No color vision           | ~0.003% (very rare) |

Machado, Oliveira & Fernandes (2009) published sRGB matrices
parameterized by severity (0 = unaffected, 1 = full dichromacy) that
simulate each deficiency. Apply the matrix in *linear RGB* (not
gamma-encoded sRGB) and you get a perceptually-grounded preview. For
achromatopsia, BT.709 luma is the standard simplification.

**Risk detection.** Simulation alone isn't actionable on a 20-
illustration library — the user needs to know *which* illustrations are
affected. The check: for each pair `(a, b)` of colors in the SVG that
are clearly distinct in the original (ΔE76 > 25), compute simulated
ΔE76. If it falls below 10, the pair has *collapsed* under the
simulation — the semantic distinction (e.g. red-as-bad vs green-as-
good) is lost. Flag the illustration for that CB type.

**Implementation.** `src/colorblind.py` ships the matrices, the
simulation, and `assess_risk()`. `app/tab_accessibility.py` runs at
the **SVG level** — for each unique color in the source SVG, compute
the simulated hex; build a substitution map; re-render via
`apply_mapping_with_report`. Vector output, no Inkscape pass per
illustration, no Ghostscript per CB type. Two modes:

* **Library strip** — every illustration in one row, columns =
  simulations. A red dot above any sim cell = "at risk for this
  audience".
* **Per-illustration grid** — pick one, see all six panes large with
  the list of collapsed pairs (original ΔE → simulated ΔE).

The `Show only affected illustrations` checkbox filters the strip down
to the dangerous subset — useful when you want a pre-delivery audit
across a 20-figure library.

CMYK soft-proof was deliberately excluded from this tab: running
Ghostscript per CB type per illustration would be prohibitively slow,
and the grayscale (achromat) sim already answers "does the ordering
survive print?".

---

## Phase 6 — Delivery snapshots

**Theory.** Six weeks after a publisher delivery, the publisher asks
for a tweak to figure 04.03. By then the global maps have moved on,
the ICC profile may have been swapped, or the active theme reshuffled.
Without a *snapshot of project state at delivery time*, reproducing the
exact delivered file is guesswork.

The snapshot is the audit trail prepress operators expect. It's purely
read-only — never consumed by the running pipeline — and self-
contained so it can be archived alongside the publisher's records.

**Implementation.** `src/delivery.py:create_snapshot()` writes
`deliveries/<UTC-timestamp>-<slug>/` containing:

* `config.json.snapshot` — paths, ICC, trim/bleed.
* `color-config.json.snapshot` — global maps, matching, print safety.
* `semantic-palette.json.snapshot` — slot bindings + active theme
  (when the file exists).
* `pdfs/` — hardlinked copies of every matching PDF + soft-proof
  PNG. Hardlinks fall back to copies on cross-device or non-link-
  capable filesystems.
* `manifest.json` — every PDF with SHA-256 + byte size.
* `README.md` — auto-generated summary table for the publisher.

Surfaced as a `python -m src.cli deliver --label "acme-2026-05"`
subcommand and a "Create delivery package" button at the bottom of the
CMYK Print Export tab. The directory name embeds an HHMMSS timestamp
so two snapshots in the same minute don't collide.

---

## Verification

44 new tests across 5 files; full suite at **248 passing**:

| Suite                  | Tests |
|------------------------|-------|
| `test_filename_template` | 21    |
| `test_force_k`         | 10    |
| `test_bleed_overlay`   | 5     |
| `test_semantic_palette`| 14    |
| `test_colorblind`      | 8     |
| `test_delivery`        | 7     |

Plus 5 new test cases in `test_svg_parser` covering the color-space
warning detector.

End-to-end smoke checks:

```powershell
& .\.venv\Scripts\python.exe -m src.cli cmyk-convert --help          # lists --filename-template
& .\.venv\Scripts\python.exe -m src.cli deliver --help               # new subcommand
& .\.venv\Scripts\python.exe -m src.cli cmyk-convert --dry-run       # plan + GS command
```

---

## Open items

* **Force-K Layer B** (sentinel-based per-color rewrite of fine lines
  to pure-K) is intentionally not implemented — the Ghostscript flags
  do the safe-and-cheap thing. Revisit if a publisher ever asks for
  near-black (not exact-black) lines forced to K.
* **TAC at low DPI** — 100 dpi may underestimate spikes from very fine
  features. Configurable via `cmyk_export.tac_check_dpi`; raise to
  150–200 if false negatives appear in real deliveries.
* **Color-blind risk thresholds** — the (25, 10) ΔE pair is a
  starting heuristic. May need tuning once we see real false-positive /
  false-negative rates on the user's library.
* **Multi-publisher profiles** — data shapes already support multiple
  themes; UI/CLI for selecting per-publisher bundles (trim + ICC + TAC
  limit + theme) is a future migration, not a rewrite.
