# Project Instructions

Canonical instructions for AI coding agents working in this repository. Claude Code reads this file directly as project memory. Other agents (Cursor, Codex, etc.) reach it via the one-line `AGENTS.md` pointer.

## Streamlit conventions
*Apply only if this project uses Streamlit.*

- `st.set_page_config(layout="wide", page_title="...")` MUST be the first Streamlit call.
- Use `width="stretch"` (and `width="content"` where appropriate) in new and modified code. **Never** introduce new `use_container_width=True` — it is deprecated. When you touch existing code that uses `use_container_width`, migrate it.
- All mutable state in `st.session_state`. No module-level globals.
- `@st.cache_data` for DataFrames/files; `@st.cache_resource` for DB clients/models.
- Every widget needs a stable, explicit `key=`.
- UI code only in the UI directory (e.g. `app/`). Data logic stays in the non-UI package (e.g. `src/`). Never import `streamlit` from non-UI code.
- User feedback via `st.error()` / `st.warning()` / `st.success()`, not `st.write()`.
- **App layout:** main file (e.g. `app.py`) handles only page config, shared state, sidebar, and tab/radio routing. Each tab/mode lives in its own file exposing a `main(...)` (or `render_*`) function. Default to `st.tabs()`; use a sidebar radio only when asked.

## This repository
Standalone Streamlit app for SVG color-to-grayscale conversion, built for book illustrations where color carries semantic meaning. Uses a two-file config: `config.json` for folder paths, `color-config.json` for color mappings and matching parameters. Source SVGs come from Affinity Designer 2 exports; output SVGs are re-imported into Affinity for the grayscale print edition.
See `README.md` for setup, layout, and usage.

## Internal architecture

[`docs/architecture.mmd`](docs/architecture.mmd) is a hand-authored Mermaid diagram of this repo's own internal structure (the Streamlit tab groups, the shared `src/` pipeline core, the CMYK print pipeline stages, `cli.py`, and the persisted/output stores) — the per-repo companion to the fleet-wide convention in `ferraroroberto/fleet-config#256`. Update it in the same PR as any material structural change (a tab added/moved, a pipeline stage added, a new persisted store). It is not auto-generated and is not covered by `pytest`.

**Project specifics:**

- **Streamlit:** this project is Streamlit.
- **Tab convention:** tab files `app/tab_*.py` expose `render_*` functions.
