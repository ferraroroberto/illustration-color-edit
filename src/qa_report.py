"""HTML QA report writer for the CMYK print export pipeline.

Produces a single self-contained HTML page summarising one batch run:

  * Run metadata (timestamp, ICC profile, dimensions, PDF/X flag).
  * Palette: every unique RGB color found across the batch, with its
    pre-corrected target (if any) shown as a side-by-side swatch.
  * Per-file table: status, replacements made, unmapped colors, warnings,
    elapsed seconds, links to the output PDF and the soft-proof PNG.

The report is plain HTML (no external assets) so it can be archived alongside
the PDF deliverables and opened on any machine.
"""

from __future__ import annotations

import html
from pathlib import Path

from .cmyk_pipeline import BatchReport, FileResult


def _swatch(hex_color: str, size_px: int = 18) -> str:
    safe = html.escape(hex_color)
    return (
        f'<span style="display:inline-block;width:{size_px}px;height:{size_px}px;'
        f'background:{safe};border:1px solid #888;vertical-align:middle;'
        f'margin-right:4px"></span>'
    )


def _fmt_path(path: Path | None, base: Path) -> str:
    if path is None:
        return "—"
    try:
        rel = path.relative_to(base)
    except ValueError:
        rel = path
    href = html.escape(str(rel).replace("\\", "/"))
    return f'<a href="{href}">{html.escape(rel.name)}</a>'


def _tac_cell(tac) -> str:
    if tac is None:
        return '<td style="color:#888">—</td>'
    color = {"ok": "#2a7", "warn": "#d97706", "fail": "#c33"}.get(tac.status, "#222")
    return (
        f'<td style="color:{color};text-align:right;font-variant-numeric:tabular-nums" '
        f'title="threshold {tac.threshold_pct:.0f}%, mean {tac.mean_pct:.1f}%, '
        f'p99 {tac.p99_pct:.1f}%, over-limit pixels {tac.violation_fraction*100:.4f}%">'
        f"{tac.max_pct:.0f}% [{tac.status}]"
        f"</td>"
    )


def _force_k_cell(fl, applied: bool) -> str:
    if fl is None:
        return '<td style="color:#888">—</td>'
    if fl.total == 0:
        return '<td style="color:#2a7">none</td>'
    badge = (
        '<span style="background:#2a7;color:#fff;border-radius:8px;padding:1px 6px;'
        'font-size:0.78em;margin-left:4px">auto-fix on</span>'
        if applied else ""
    )
    return (
        f'<td title="strokes={fl.stroke_count}, text={fl.text_count}">'
        f"{fl.stroke_count} stroke / {fl.text_count} text{badge}"
        f"</td>"
    )


def render_report(report: BatchReport, output_dir: Path) -> str:
    """Return the QA report as an HTML string. Caller writes to disk."""
    rows: list[str] = []
    for f in report.files:
        status_color = "#2a7" if f.status == "ok" else "#c33"
        warnings_html = (
            "<br>".join(html.escape(w) for w in f.warnings) if f.warnings else "—"
        )
        unmapped_html = (
            ", ".join(_swatch(c, 12) + html.escape(c) for c in f.unmapped_colors[:12])
            + (f" <em>(+{len(f.unmapped_colors)-12} more)</em>" if len(f.unmapped_colors) > 12 else "")
            if f.unmapped_colors
            else "—"
        )
        error_html = html.escape(f.error) if f.error else ""
        rows.append(
            f"<tr>"
            f"<td>{html.escape(f.filename)}</td>"
            f'<td style="color:{status_color};font-weight:600">{html.escape(f.status)}</td>'
            f'<td style="text-align:right">{f.replacements}</td>'
            f"{_tac_cell(f.tac)}"
            f"{_force_k_cell(f.fine_lines, f.auto_fix_applied)}"
            f"<td>{unmapped_html}</td>"
            f"<td>{warnings_html}</td>"
            f'<td style="text-align:right">{f.elapsed_seconds:.2f}s</td>'
            f"<td>{_fmt_path(f.output_pdf, output_dir)}</td>"
            f"<td>{_fmt_path(f.preview_png, output_dir)}</td>"
            f'<td style="color:#c33">{error_html}</td>'
            f"</tr>"
        )

    palette_rows: list[str] = []
    for src in sorted(report.palette):
        count = report.palette[src]
        target = report.palette_mapped.get(src.upper())
        if target and target.upper() != src.upper():
            mapping_cell = (
                f"{_swatch(src)}<code>{html.escape(src)}</code> → "
                f"{_swatch(target)}<code>{html.escape(target)}</code>"
            )
        else:
            mapping_cell = f"{_swatch(src)}<code>{html.escape(src)}</code> (unchanged)"
        palette_rows.append(
            f'<tr><td>{mapping_cell}</td><td style="text-align:right">{count}</td></tr>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CMYK QA report — {html.escape(report.started_at)}</title>
<style>
  body {{ font: 14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif; color:#222; padding:24px; max-width:1400px; margin:0 auto }}
  h1 {{ margin-top:0 }}
  table {{ border-collapse:collapse; width:100%; margin:12px 0 28px }}
  th, td {{ padding:6px 10px; border-bottom:1px solid #ddd; vertical-align:top }}
  th {{ background:#f5f5f5; text-align:left; font-weight:600 }}
  .meta {{ background:#fafafa; padding:12px 16px; border-radius:6px; margin-bottom:24px }}
  .meta dt {{ float:left; clear:left; width:170px; color:#666 }}
  .meta dd {{ margin:0 0 4px 170px }}
  code {{ font:13px Consolas,Menlo,monospace }}
  .summary {{ display:inline-block; margin-right:24px; font-weight:600 }}
  .ok {{ color:#2a7 }}
  .err {{ color:#c33 }}
</style>
</head>
<body>
<h1>CMYK QA report</h1>
<div class="meta">
  <dl>
    <dt>Started</dt><dd>{html.escape(report.started_at)}</dd>
    <dt>Finished</dt><dd>{html.escape(report.finished_at)}</dd>
    <dt>Elapsed</dt><dd>{report.total_seconds:.2f}s</dd>
    <dt>ICC profile</dt><dd><code>{html.escape(report.icc_profile)}</code></dd>
    <dt>PDF/X-1a</dt><dd>{'enabled' if report.pdfx else 'disabled'}</dd>
    <dt>Trim size</dt><dd>{report.width_inches:.3f} × {report.height_inches:.3f} in (bleed {report.bleed_inches:.3f} in)</dd>
  </dl>
</div>
<p>
  <span class="summary ok">{report.succeeded} succeeded</span>
  <span class="summary err">{report.failed} failed</span>
  <span class="summary">{len(report.files)} total</span>
</p>

<h2>Palette</h2>
<table>
  <thead><tr><th>Source → corrected target</th><th style="text-align:right">files using</th></tr></thead>
  <tbody>{''.join(palette_rows) or '<tr><td colspan="2" style="color:#888">No colors extracted.</td></tr>'}</tbody>
</table>

<h2>Per-file results</h2>
<table>
  <thead><tr>
    <th>file</th><th>status</th><th style="text-align:right">replacements</th>
    <th style="text-align:right">TAC max</th><th>force-K</th>
    <th>unmapped colors</th><th>warnings</th>
    <th style="text-align:right">elapsed</th>
    <th>PDF</th><th>preview</th><th>error</th>
  </tr></thead>
  <tbody>{''.join(rows) or '<tr><td colspan="11" style="color:#888">No files processed.</td></tr>'}</tbody>
</table>
</body>
</html>
"""


def write_report(report: BatchReport, output_dir: Path, filename: str = "cmyk_qa_report.html") -> Path:
    """Render and write the QA report. Returns the path written."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    path.write_text(render_report(report, output_dir), encoding="utf-8")
    return path
