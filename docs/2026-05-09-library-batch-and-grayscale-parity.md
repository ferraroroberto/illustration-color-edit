# 2026-05-09 — Library batch ops, grayscale ↔ CMYK parity, settings symmetry

## What was done

The CMYK side had accumulated several UX/correctness improvements that
the grayscale side never received. This pass closes the gap, plus adds
the long-asked Library batch-wipe.

### Library batch operations
- `tab_library.py` rewritten to use `st.dataframe` with native multi-row
  selection. Columns: File, Gray, Gray ovr, CMYK, CMYK ovr, Size KB,
  Modified.
- Action bar drives off the dataframe selection: *Open* (single),
  *Wipe grayscale (N)*, *Wipe CMYK (N)*.
- *Wipe ALL* expander gated by a confirm checkbox for the
  whole-library reset case.
- `LibraryEntry` extended with `cmyk_status` and `cmyk_override_count`.
- `LibraryManager.cmyk_status_counts()` added; header shows badge
  rows for both pipelines.

### MappingStore: bulk wipe + generalized cleanup
- `wipe_pipeline(filenames, pipeline)` clears per-file overrides and
  resets that pipeline's status to `pending` for the listed files.
  When both pipelines end up empty + pending (and notes empty), the
  metadata file is deleted outright so `metadata/` doesn't accumulate
  empty stubs.
- `cleanup_identity_entries(pipeline=...)` now accepts
  `"grayscale" | "cmyk" | "both"` (default `"cmyk"` for back-compat
  with the existing CMYK Settings button).

### Grayscale Editor parity
- `_persistable_overrides` ported from the CMYK editor: drops identity
  picks (`target == source`) and picks that already match
  `global_color_map[src].target`. Applied on Save / Save & mark
  reviewed / Promote ALL.
- Per-row **↺ reset** clears that color's per-file override and its
  global-map entry in one click, plus the three associated
  session-state keys.
- *📂 Open output folder* button on the Editor action row, matching
  the CMYK editor.

### Shared editor helpers
- `_HEX_RE`, `normalize_hex`, `apply_hex_input` moved from both editor
  modules into `app/common.py`. Both editors now import the shared
  versions.

### Grayscale Settings tab
- `tab_settings.py` extended from a read-only dump into a full Settings
  tab matching `tab_cmyk_settings.py`'s shape: read-only summary,
  editable form (paths + png_export → `config.json`; matching +
  print_safety → `color-config.json`), and a Maintenance section with
  the new *Clean identity entries from all grayscale metadata* button.

### README + docs
- README documents the new Library batch ops, the grayscale editor's
  reset/identity behaviors, and the two-step grayscale → CMYK manual
  workflow (point `input_dir` at `./output`, run `cmyk-convert`,
  restore).

## Files modified

```
src/library_manager.py            # +cmyk_status, +cmyk_override_count, +cmyk_status_counts
src/mapping_store.py              # +wipe_pipeline, generalized cleanup_identity_entries
app/tab_library.py                # rewrite: dataframe + multi-row selection + action bar
app/tab_editor.py                 # +_persistable_overrides, +↺ reset, +open-output button
app/tab_cmyk_editor.py            # use shared hex helpers
app/common.py                     # +normalize_hex, +apply_hex_input
app/tab_settings.py               # rewrite: editable form + Maintenance
tests/test_mapping_store_cmyk.py  # +tests for wipe_pipeline and pipeline= parameter
README.md                         # document Library batch ops + two-step workflow
```

## What was NOT touched (deliberately, per scope agreed with user)

- No deduplication of the twin global-map tabs (`tab_global_map.py` ↔
  `tab_cmyk_global_map.py`) or the twin `MappingStore.*global*` /
  `*cmyk_correction*` methods. Marked for a future pass when the
  benefit is clear; not blocking anything today.
- No editor-tab merge. The grayscale and CMYK editors look similar but
  the printing-safety/luminance branches and the soft-proof column
  diverge enough that a merge would be more friction than savings.
- No CMYK-source-dir toggle. The two-step workflow is documented as a
  manual `input_dir` swap; user explicitly preferred this over a new
  setting.

## Validation

```
& .\.venv\Scripts\python.exe -m pytest tests/test_mapping_store.py tests/test_mapping_store_cmyk.py -q
# 35 + new wipe/cleanup tests pass

& .\.venv\Scripts\python.exe -m py_compile app/*.py src/*.py
# clean
```

Manual UI exercise (recommended):
1. Library — select 2 rows, *Wipe CMYK*, confirm CMYK columns reset
   while Gray columns are preserved.
2. Editor — pick a color, hit *Save* with `target==source`; reload —
   should not have been persisted.
3. Editor — click ↺ on a row that has both override and global entry;
   confirm both vanish from `metadata/<file>.mapping.json` and from
   `color-config.json` `global_color_map`.
4. Settings — change PNG DPI, click *Save paths + PNG*; reopen
   `config.json` to verify.
